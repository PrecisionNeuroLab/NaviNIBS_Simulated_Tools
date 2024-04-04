import asyncio
import attrs
import logging
import numpy as np
import typing as tp
import zmq
import zmq.asyncio as azmq

from RTNaBS.Devices.ToolPositionsClient import ToolPositionsClient


logger = logging.getLogger(__name__)


@attrs.define
class SimulatedToolPositionsClient(ToolPositionsClient):

    _readyToCheckType: asyncio.Event = attrs.field(init=False, factory=asyncio.Event)

    def __attrs_post_init__(self):
        super().__attrs_post_init__()
        self.sigIsConnectedChanged.connect(lambda: self._readyToCheckType.set() if self._isConnected else self._readyToCheckType.clear())

    async def _checkServerType(self):
        while True:
            await self._readyToCheckType.wait()
            self._readyToCheckType.clear()

            serverType = self._connector.get('type')
            if serverType != 'Simulated':
                logger.error('Tried to use SimulatedToolPositionsClient to connect to non-simulated ToolPositionsServer')
