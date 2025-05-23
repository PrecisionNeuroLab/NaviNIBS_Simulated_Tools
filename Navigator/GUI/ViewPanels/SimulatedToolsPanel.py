from __future__ import annotations

import asyncio

import attrs
from datetime import datetime
import json
import logging
import numpy as np
import os
import pyvista as pv
import qtawesome as qta
from qtpy import QtWidgets, QtGui
import time
import typing as tp


from NaviNIBS.Devices import TimestampedToolPosition
from NaviNIBS_Simulated_Tools.Devices.SimulatedToolPositionsClient import SimulatedToolPositionsClient
from NaviNIBS_Simulated_Tools.Navigator.Model.SimulatedToolsConfiguration import SimulatedTools as SimulatedToolsConfig, SimulatedToolPose
from NaviNIBS.Navigator.Model.Session import SubjectTracker
from NaviNIBS.Navigator.GUI.Widgets.TrackingStatusWidget import TrackingStatusWidget
from NaviNIBS.Navigator.GUI.ViewPanels.MainViewPanelWithDockWidgets import MainViewPanelWithDockWidgets
from NaviNIBS.util.Asyncio import asyncTryAndLogExceptionOnError
from NaviNIBS.util.GUI.Icons import getIcon
from NaviNIBS.util.json import jsonPrettyDumps
from NaviNIBS.util.pyvista import Actor, setActorUserTransform, RemotePlotterProxy
from NaviNIBS.util.pyvista.PlotInteraction import pickActor, interactivelyMoveActor
from NaviNIBS.util.Transforms import invertTransform, concatenateTransforms
from NaviNIBS.util.pyvista.plotting import BackgroundPlotter


logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


@attrs.define
class SimulatedToolsPanel(MainViewPanelWithDockWidgets):
    _label: str = 'Simulated tools'
    _icon: QtGui.QIcon = attrs.field(init=False, factory=lambda: getIcon('mdi6.progress-wrench'))
    _trackingStatusWdgt: TrackingStatusWidget = attrs.field(init=False)
    _plotter: BackgroundPlotter = attrs.field(init=False)
    _actors: tp.Dict[str, tp.Optional[Actor]] = attrs.field(init=False, factory=dict)

    _currentlyMovingActors: set[str] = attrs.field(init=False, factory=set)

    _positionsClient: SimulatedToolPositionsClient = attrs.field(init=False)

    _hasRestoredPositions: bool = attrs.field(init=False, default=False)

    finishedAsyncInit: asyncio.Event = attrs.field(init=False, factory=asyncio.Event)

    def __attrs_post_init__(self):
        super().__attrs_post_init__()

    @property
    def positionsClient(self):
        return self._positionsClient

    def _onSessionSet(self):
        super()._onSessionSet()

        # initialize right away so will start listening to pose updates
        self._positionsClient = SimulatedToolPositionsClient(
            serverHostname=self.session.tools.positionsServerInfo.hostname,
            serverPubPort=self.session.tools.positionsServerInfo.pubPort,
            serverCmdPort=self.session.tools.positionsServerInfo.cmdPort,
        )
        # TODO: reconnect positions client if positionsServerInfo changes later
        self._positionsClient.sigLatestPositionsChanged.connect(self._onLatestPositionsChanged)

        self._onLatestPositionsChanged()

    def canBeEnabled(self) -> tuple[bool, str | None]:
        if self.session is None:
            return False, 'No session set'
        return True, None

    def _finishInitialization(self):
        super()._finishInitialization()

        self._trackingStatusWdgt = TrackingStatusWidget(session=self._session, wdgt=QtWidgets.QWidget())
        dock, _ = self._createDockWidget(
            title='Tracking status',
            widget=self._trackingStatusWdgt.wdgt)
        dock.setStretch(1, 10)
        dock.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Maximum)
        self._wdgt.addDock(dock, position='left')

        dock, container = self._createDockWidget(
            title='Simulated tool controls',
            layout=QtWidgets.QVBoxLayout()
        )
        dock.setStretch(1, 10)
        self._wdgt.addDock(dock, position='bottom')

        btn = QtWidgets.QPushButton('Clear all positions')
        btn.clicked.connect(lambda checked: self.clearAllPositions())
        container.layout().addWidget(btn)

        btn = QtWidgets.QPushButton('Zero all positions')
        btn.clicked.connect(lambda checked: self.zeroAllPositions())
        container.layout().addWidget(btn)

        btn = QtWidgets.QPushButton('Move tool...')
        btn.clicked.connect(lambda checked: self.selectToolToMove())
        container.layout().addWidget(btn)

        btn = QtWidgets.QPushButton('Clear tool position...')
        btn.clicked.connect(lambda checked: self.selectToolToClearPos())
        container.layout().addWidget(btn)

        container.layout().addSpacing(10)

        btn = QtWidgets.QPushButton('Import positions snapshot...')
        btn.clicked.connect(lambda checked: asyncio.create_task(asyncTryAndLogExceptionOnError(self.importPositionsSnapshot)))
        container.layout().addWidget(btn)

        btn = QtWidgets.QPushButton('Export positions snapshot...')
        btn.clicked.connect(lambda checked: asyncio.create_task(asyncTryAndLogExceptionOnError(self.exportPositionsSnapshot)))
        container.layout().addWidget(btn)

        container.layout().addStretch()

        logger.debug(f'Preparing background plotter')

        self._plotter = BackgroundPlotter()
        self._plotter.enable_depth_peeling(3)
        if False:
            # (disabled for now, breaks mesh picking)
            self._plotter.add_axes_at_origin(labels_off=True, line_width=4)
        dock, container = self._createDockWidget(
            title='Simulated tools view',
            widget=self._plotter
        )
        self._wdgt.addDock(dock, position='right')

        self._trackingStatusWdgt.session = self.session
        self._onLatestPositionsChanged()

        self._session.tools.sigItemsChanged.connect(self._onToolsChanged)

        asyncio.create_task(asyncTryAndLogExceptionOnError(self._finishInitialization_async))

    async def _finishInitialization_async(self):
        if isinstance(self._plotter, RemotePlotterProxy):
            await self._plotter.isReadyEvent.wait()

        self.finishedAsyncInit.set()

    def _onToolsChanged(self, toolKeysChanged: tp.List[str], changedAttribs: tp.Optional[list[str]] = None):
        didRemove = False
        for key, tool in self.session.tools.items():
            actorKeysForTool = [key + '_tracker', key + '_tool']
            for actorKey in actorKeysForTool:
                if actorKey in self._actors:
                    self._plotter.remove_actor(self._actors[actorKey])
                    self._actors.pop(actorKey)
                    didRemove = True

        if didRemove:
            self._onLatestPositionsChanged()

    def _onLatestPositionsChanged(self):

        config: SimulatedToolsConfig = self.session.addons['NaviNIBS_Simulated_Tools'].SimulatedTools

        if not self._hasRestoredPositions:
            # restore previously-saved positions on startup
            for trackerKey, pose in config.poses.items():
                if pose.transf is None:
                    continue

                # only restore positions without a position already set
                try:
                    self._positionsClient.getLatestTransf(trackerKey)
                except KeyError as e:
                    # TODO: maybe make this async, after verifying won't cause concurrency problems
                    self._positionsClient.recordNewPosition_sync(
                        key=trackerKey,
                        position=TimestampedToolPosition(
                            time=time.time(),
                            transf=pose.transf,
                            relativeTo=pose.relativeTo,
                        ))

            self._hasRestoredPositions = True

        for key, tool in self.session.tools.items():
            transf = self._positionsClient.getLatestTransf(tool.trackerKey, None)
            if transf is None and tool.trackerKey not in config.poses:
                # don't add now
                pass
            else:
                if tool.trackerKey not in config.poses:
                    config.poses[tool.trackerKey] = SimulatedToolPose(key=tool.trackerKey)
                config.poses[tool.trackerKey].transf = transf

        if not self._hasInitialized and not self.isInitializing:
            return

        doResetCamera = False

        for key, tool in self.session.tools.items():
            if True == False:
                logger.debug('TODO: delete')
            actorKeysForTool = [key + '_tracker', key + '_tool']
            if isinstance(tool, SubjectTracker):
                actorKeysForTool.append(key + '_subject')

            if not tool.isActive or self._positionsClient.getLatestTransf(tool.trackerKey, None) is None:
                # no valid position available
                for actorKey in actorKeysForTool:
                    if actorKey in self._actors and self._actors[actorKey].GetVisibility():
                        self._actors[actorKey].VisibilityOff()
                continue

            for actorKey in actorKeysForTool:
                #logger.debug(f'actorKey: {actorKey}')

                if actorKey in self._currentlyMovingActors:
                    # don't update position of currently moving actor to avoid "flickering"
                    continue

                doShow = False
                for toolOrTracker in ('tracker', 'tool'):
                    if actorKey == (key + '_' + toolOrTracker):
                        if getattr(tool, 'doRender' + toolOrTracker.capitalize()) is False:
                            doShow = False
                        else:
                            if getattr(tool, toolOrTracker + 'StlFilepath') is not None:
                                if toolOrTracker == 'tool':
                                    if tool.toolToTrackerTransf is None:
                                        toolOrTrackerStlToTrackerTransf = None
                                    else:
                                        toolOrTrackerStlToTrackerTransf = tool.toolToTrackerTransf @ tool.toolStlToToolTransf
                                elif toolOrTracker == 'tracker':
                                    toolOrTrackerStlToTrackerTransf = tool.trackerStlToTrackerTransf
                                else:
                                    raise NotImplementedError()
                                if toolOrTrackerStlToTrackerTransf is not None:
                                    doShow = True
                                    if actorKey not in self._actors:
                                        # initialize graphic
                                        mesh = getattr(tool, toolOrTracker + 'Surf')
                                        meshColor = tool.trackerColor if toolOrTracker == 'tracker' else tool.toolColor
                                        meshOpacity = tool.trackerOpacity if toolOrTracker == 'tracker' else tool.toolOpacity

                                        self._actors[actorKey] = self._plotter.addMesh(mesh=mesh,
                                                                                       color=meshColor,
                                                                                       defaultMeshColor='#444444',
                                                                                       opacity=1.0 if meshOpacity is None else meshOpacity,
                                                                                       name=actorKey)

                                        doResetCamera = True

                                    # apply transform to existing actor
                                    setActorUserTransform(self._actors[actorKey],
                                                          concatenateTransforms([
                                                              toolOrTrackerStlToTrackerTransf,
                                                              self._positionsClient.getLatestTransf(tool.trackerKey)
                                                          ]))
                                    self._plotter.render()
                            else:
                                # TODO: show some generic graphic to indicate tool position, even when we don't have an stl for the tool
                                doShow = False

                if isinstance(tool, SubjectTracker) and actorKey == tool.key + '_subject':
                    if self.session.subjectRegistration.trackerToMRITransf is not None and self.session.headModel.skinSurf is not None:
                        doShow = True
                        if actorKey not in self._actors:
                            self._actors[actorKey] = self._plotter.add_mesh(mesh=self.session.headModel.skinSurf,
                                                                            color='#d9a5b2',
                                                                            opacity=0.8,
                                                                            name=actorKey)
                            doResetCamera = True

                        setActorUserTransform(self._actors[actorKey],
                                              self._positionsClient.getLatestTransf(tool.trackerKey) @ invertTransform(self.session.subjectRegistration.trackerToMRITransf))
                        self._plotter.render()

                if actorKey in self._actors:
                    if doShow and not self._actors[actorKey].GetVisibility():
                        self._actors[actorKey].VisibilityOn()
                        self._plotter.render()
                    elif not doShow and self._actors[actorKey].GetVisibility():
                        self._actors[actorKey].VisibilityOff()
                        self._plotter.render()

        if doResetCamera:
            pass  # self._plotter.reset_camera()

    def clearAllPositions(self):
        for key, tool in self.session.tools.items():
            self._positionsClient.recordNewPosition_sync(
                key=tool.trackerKey,
                position=TimestampedToolPosition(
                    time=time.time(),
                    transf=None
                )
            )

    def zeroAllPositions(self):
        for key, tool in self.session.tools.items():
            if True:
                # only zero positions for tools that don't have positions defined relative to another tool
                pos = self._positionsClient.latestPositions.get(tool.trackerKey, None)
                if pos is not None and pos.relativeTo is not None:
                    continue

            self._positionsClient.recordNewPosition_sync(key=tool.trackerKey,
                                                         position=TimestampedToolPosition(
                                                                time=time.time(),
                                                                transf=np.eye(4),
                                                         ))

    async def importPositionsSnapshot(self, filepath: str | None = None, positionsDict: dict[str, dict] | None = None):

        if positionsDict is None:
            if filepath is None:
                filepath, _ = QtWidgets.QFileDialog.getOpenFileName(self._wdgt,
                                                                    'Select positions snapshot to import')
                # filepath, _ = QtWidgets.QFileDialog.getOpenFileName(self._wdgt,
                #                                                  'Select positions snapshot to import',
                #                                                  self.session.unpackedSessionDir,
                #                                                  'json (*.json)')

            if len(filepath) == 0:
                logger.warning('Import cancelled')
                return

            with open(filepath, 'r') as f:
                positionsDict: dict[str, dict] = json.load(f)
        else:
            positionsDict = positionsDict.copy()

        for key in positionsDict.keys():
            positionsDict[key] = TimestampedToolPosition.fromDict(positionsDict[key])

        for key, tsPos in positionsDict.items():
            tsPos.time = time.time()  # overwrite old time to make this look like a "new" position
            logger.info('Setting position for ' + key + ' to ' + str(tsPos.transf))
            if True:
                await self._positionsClient.recordNewPosition_async(key=key, position=tsPos)
            else:
                self._positionsClient.recordNewPosition_sync(key=key, position=tsPos)

    async def exportPositionsSnapshot(self, filepath: str | None = None,
                                doIncludeToolsWithRelativePositions: bool = False):

        if filepath is None:
            filepath, _ = QtWidgets.QFileDialog.getSaveFileName(self._wdgt,
                                                             'Select positions snapshot to export',
                                                             os.path.join(self.session.unpackedSessionDir,
                                                                          'SimulatedPositions_' + datetime.today().strftime('%y%m%d%H%M%S')),
                                                             'json (*.json)')

        if len(filepath) == 0:
            logger.warning('Export cancelled')
            return

        positions = {key: tsPos.asDict() for key, tsPos in self._positionsClient.latestPositions.items()}

        if not doIncludeToolsWithRelativePositions:
            for key in list(positions.keys()):
                tsPosDict = positions[key]
                if tsPosDict.get('relativeTo', None) is not None:
                    del positions[key]

        toWrite = jsonPrettyDumps(positions)
        with open(filepath, 'w') as f:
            f.write(toWrite)

        logger.info(f'Exported positions snapshot to {filepath}')

    async def clearToolPos(self, toolKey: str):
        logger.info(f'Clearing position of {toolKey}')

        await self._positionsClient.recordNewPosition_async(
            key=self.session.tools[toolKey].trackerKey,
            position=TimestampedToolPosition(
                time=time.time(),
                transf=None
            )
        )

    async def selectAndClearToolPos(self, toolKey: str | None = None):
        if toolKey is None:
            # start by picking mesh to move
            pickedActor = await pickActor(self._plotter,
                                          show=True,
                                          show_message='Left click on mesh to clear',
                                          style='wireframe',
                                          left_clicking=True)
            try:
                pickedKey = [actorKey for actorKey, actor in self._actors.items() if actor is pickedActor][0]
            except IndexError as e:
                logger.warning('Unrecognized actor picked. Cancelling select')
                return
            if pickedKey.endswith('_tracker'):
                pickedTool = self.session.tools[pickedKey[:-len('_tracker')]]
            elif pickedKey.endswith('_tool'):
                pickedTool = self.session.tools[pickedKey[:-len('_tool')]]
            else:
                raise NotImplementedError

            toolKey = pickedTool.key

        await self.clearToolPos(toolKey)

    async def selectAndMoveTool(self, pickedActor: Actor | None = None):
        if pickedActor is None:
                # start by picking mesh to move
                pickedActor = await pickActor(self._plotter,
                                              show=True,
                                              show_message='Left click on mesh to move',
                                              style='wireframe',
                                              left_clicking=True)
        try:
            pickedKey = [actorKey for actorKey, actor in self._actors.items() if actor is pickedActor][0]
        except IndexError as e:
            logger.warning('Unrecognized actor picked. Cancelling select and move')
            return
        if pickedKey.endswith('_tracker'):
            pickedTool = self.session.tools[pickedKey[:-len('_tracker')]]
        elif pickedKey.endswith('_tool'):
            pickedTool = self.session.tools[pickedKey[:-len('_tool')]]
        else:
            raise NotImplementedError
        logger.info(f'Picked actor {pickedKey} ({pickedTool.key}) to move')

        assert pickedKey not in self._currentlyMovingActors
        self._currentlyMovingActors.add(pickedKey)

        # move
        def onNewTransf(transf: pv._vtk.vtkTransform):
            prevTransf = pickedActor.GetUserTransform()
            logger.debug('onNewTransf')
            # back out any tool-specific transforms and send updated transf to simulated tool position server
            transf = pv.array_from_vtkmatrix(transf.GetMatrix())
            if pickedKey.endswith('_tool'):
                # transf is toolStlToWorldTransf
                newTrackerToWorldTransf = concatenateTransforms([
                    invertTransform(concatenateTransforms([
                        pickedTool.toolStlToToolTransf,
                        pickedTool.toolToTrackerTransf
                    ])),
                    transf,
                ])
            elif pickedKey.endswith('_tracker'):
                # transf is trackerStlToWorldTransf
                newTrackerToWorldTransf = concatenateTransforms([
                    invertTransform(pickedTool.trackerStlToTrackerTransf),
                    transf,
                ])
            else:
                msg = f'Support for moving {pickedKey} not yet implemented'
                logger.error(msg)
                raise NotImplementedError(msg)

            logger.info(f'Setting new simulated position: {newTrackerToWorldTransf}')
            self._positionsClient.recordNewPosition_sync(
                key=pickedTool.trackerKey,
                position=TimestampedToolPosition(
                    time=time.time(),
                    transf=newTrackerToWorldTransf
                )
            )
            logger.debug('Done setting new simulated position')

        await interactivelyMoveActor(plotter=self._plotter, actor=pickedActor, onNewTransf=onNewTransf)

        self._currentlyMovingActors.remove(pickedKey)

        # TODO: cleanup here



    def selectToolToMove(self):
        asyncio.create_task(self.selectAndMoveTool())

    def selectToolToClearPos(self):
        asyncio.create_task(self.selectAndClearToolPos())
