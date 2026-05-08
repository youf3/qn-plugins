import os
import sys
import json
import asyncio
from quantnet_mq.rpcclient import RPCClient
from quantnet_mq.schema.models import Schema


class MyQFC():
    def __init__(self, nodes, rate, duration):
        self._nodes = nodes
        self._rate = int(rate)
        self._duration = int(duration)

    async def do_qfc(self):
        msg = {"nodes": self._nodes, "rate": self._rate, "duration": self._duration, 'exp_param': {"use_db": True}}
        return json.loads(await self._client.call("qfcRequest", msg, timeout=20.0))

    async def do_query(self, rid):
        msg = {"rid": rid}
        return json.loads(await self._client.call("qfcQuery", msg, timeout=20.0))

    async def main(self):
        Schema.load_schema("./plugins/schema/qfc.yaml", ns="qfc")
        self._client = RPCClient("qfc-client", host=os.getenv("HOST", "localhost"))
        self._client.set_handler("qfcRequest", None, "quantnet_mq.schema.models.qfc.qfcRequest")
        self._client.set_handler("qfcQuery", None, "quantnet_mq.schema.models.qfc.qfcQuery")
        await self._client.start()
        ret = await self.do_qfc()
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
        nodes = ["LBNL-Q"]
        rate = 100
        duration = 30
    asyncio.run(MyQFC(nodes, rate, duration).main())
