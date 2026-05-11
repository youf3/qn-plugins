import os
import sys
import json
import asyncio
from quantnet_mq.rpcclient import RPCClient
from quantnet_mq.schema.models import Schema


class MyBSM():
    def __init__(self, nodes, rate, duration):
        self._nodes = nodes
        self._rate = int(rate)
        self._duration = int(duration)

    async def do_bsm(self):
        msg = {"nodes": self._nodes, "rate": self._rate, "duration": self._duration, 'exp_param': {"use_db": True}}
        return json.loads(await self._client.call("bsmRequest", msg, timeout=20.0))

    async def do_query(self, rid):
        msg = {"rid": rid}
        return json.loads(await self._client.call("bsmQuery", msg, timeout=20.0))

    async def main(self):
        Schema.load_schema("../schema/bsm.yaml", ns="bsm")
        self._client = RPCClient("bsm-client", host=os.getenv("HOST", "localhost"))
        self._client.set_handler("bsmRequest", None, "quantnet_mq.schema.models.bsm.bsmRequest")
        self._client.set_handler("bsmQuery", None, "quantnet_mq.schema.models.bsm.bsmQuery")
        await self._client.start()
        ret = await self.do_bsm()
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
        nodes = ["LBNL-Q", "UCB-Q", "LBNL-BSM"]
        rate = 100
        duration = 30
    asyncio.run(MyBSM(nodes, rate, duration).main())
