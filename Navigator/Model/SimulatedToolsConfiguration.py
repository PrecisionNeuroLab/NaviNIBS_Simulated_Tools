from __future__ import annotations
import asyncio
import attrs
import numpy as np
import typing as tp

from RTNaBS.Navigator.Model.Addons import AddonSessionConfig
from RTNaBS.Navigator.Model.Session import Session
from RTNaBS.Navigator.Model.GenericCollection import GenericCollection, GenericCollectionDictItem
from RTNaBS.util.attrs import attrsAsDict
from RTNaBS.util.numpy import attrsWithNumpyAsDict, attrsWithNumpyFromDict, array_equalish


@attrs.define
class SimulatedToolPose(GenericCollectionDictItem[str]):
    _transf: np.ndarray | None = None
    _relativeTo: str = 'world'

    def __attrs_post_init__(self):
        super().__attrs_post_init__()

    @property
    def transf(self):
        return self._transf

    @transf.setter
    def transf(self, newTransf: np.ndarray | None):
        if array_equalish(self._transf, newTransf):
            return
        self.sigItemAboutToChange.emit(self.key, ['transf'])
        self._transf = newTransf
        self.sigItemChanged.emit(self.key, ['transf'])

    @property
    def relativeTo(self):
        return self._relativeTo

    @relativeTo.setter
    def relativeTo(self, newRelativeTo: str):
        if self._relativeTo == newRelativeTo:
            return
        self.sigItemAboutToChange.emit(self.key, ['relativeTo'])
        self._relativeTo = newRelativeTo
        self.sigItemChanged.emit(self.key, ['relativeTo'])

    def asDict(self) -> tp.Dict[str, tp.Any]:
        d = attrsWithNumpyAsDict(self, npFields=('transf',))
        return d

    @classmethod
    def fromDict(cls, d: tp.Dict[str, tp.Any]):
        return attrsWithNumpyFromDict(cls, d, npFields=('transf',))


@attrs.define
class SimulatedToolPoses(GenericCollection[str, SimulatedToolPose]):
    def __attrs_post_init__(self):
        super().__attrs_post_init__()

    @classmethod
    def fromList(cls, itemList: list[dict[str, tp.Any]]) -> SimulatedToolPoses:
        items = {}
        for itemDict in itemList:
            items[itemDict['key']] = SimulatedToolPose.fromDict(itemDict)

        return cls(items=items)


@attrs.define
class SimulatedTools(AddonSessionConfig):
    _doPersistPoses: bool = True
    _poses: SimulatedToolPoses = attrs.field(factory=SimulatedToolPoses)

    def __attrs_post_init__(self):
        super().__attrs_post_init__()

        self._poses.sigItemsAboutToChange.connect(lambda keys, changedAttrs = None: self.sigConfigAboutToChange.emit(('poses',)))
        self._poses.sigItemsChanged.connect(lambda keys, changedAttrs = None: self.sigConfigChanged.emit(('poses',)))

    @property
    def poses(self) -> SimulatedToolPoses:
        return self._poses

    def asDict(self) -> dict[str, tp.Any]:
        d = attrsAsDict(self, exclude=['poses'])
        d['poses'] = self._poses.asList()
        return d

    @classmethod
    def fromDict(cls, d: dict[str, tp.Any]):
        if 'poses' in d:
            d['poses'] = SimulatedToolPoses.fromList(d['poses'])
        return cls(**d)
