import os
import sys
import json
import asyncio
from quantnet_mq.rpcclient import RPCClient
from quantnet_mq.schema.models import Schema


class MyLinkCal():
    def __init__(self, nodes, rate, duration):
        self._nodes = nodes
        self._rate = int(rate)
        self._duration = int(duration)

    async def do_linkCal(self):
        msg = {"nodes": self._nodes, "rate": self._rate, "duration": self._duration, 'exp_param': {"use_db": True}}
        return json.loads(await self._client.call("linkCalRequest", msg, timeout=20.0))

    async def do_query(self, rid):
        msg = {"rid": rid}
        return json.loads(await self._client.call("linkCalQuery", msg, timeout=20.0))

    async def main(self):
        Schema.load_schema("./plugins/schema/linkCalibration.yaml", ns="linkCal")
        self._client = RPCClient("linkCal-client", host=os.getenv("HOST", "localhost"))
        self._client.set_handler("linkCalRequest", None, "quantnet_mq.schema.models.linkCal.linkCalRequest")
        self._client.set_handler("linkCalQuery", None, "quantnet_mq.schema.models.linkCal.linkCalQuery")
        await self._client.start()
        ret = await self.do_linkCal()
        print(ret)
        await asyncio.sleep(1)
        ret = await self.do_query(ret.get("rid", ""))
        print(ret)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        nodes = sys.argv[1]
        rate = sys.argv[2]
        duration = sys.argv[3]
    else:
        nodes = ["LBNL-Q", "UCB-Q"]
        rate = 100
        duration = 30
    asyncio.run(MyLinkCal(nodes, rate, duration).main())
