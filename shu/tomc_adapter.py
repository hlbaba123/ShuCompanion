"""Optional installed TOMC MCP client, isolated by a hard process deadline.

No notebook writes and no host hooks. Only task-owned, redacted visible history
is sent to the local CPU service. Neither output nor stats grant any authority.
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile

_CLIENT = r'''
import asyncio,json,sys
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client
async def main():
    value=json.load(sys.stdin)
    async with stdio_client(StdioServerParameters(command=sys.executable,args=[value.pop('server')])) as (r,w):
        async with ClientSession(r,w) as session:
            await session.initialize()
            result=await session.call_tool('prepare_context',value)
            for item in result.content:
                if item.type=='text':
                    data=json.loads(item.text)
                    if isinstance(data.get('memory'),str):
                        print(json.dumps({'memory':data['memory']},ensure_ascii=False))
                        return
asyncio.run(main())
'''


class TomcPreparer:
    def __init__(self,root,*,timeout=8):
        self.root=Path(root)
        self.python=self.root/'.venv/Scripts/python.exe'
        self.server=self.root/'src/server.py'
        self.timeout=timeout

    @classmethod
    def installed(cls):
        adapter=cls(Path.home()/'.codex/tools/tomc-memory-0.3.1')
        return adapter if adapter.python.is_file() and adapter.server.is_file() else None

    def __call__(self,*,history,task,task_id):
        if not task_id or not isinstance(history,str) or len(history)>100000:
            raise ValueError('TOMC task context input is invalid')
        # Capture in a local file: communicate() on Windows may wait forever
        # for an MCP descendant holding a pipe open after its parent is killed.
        # Do not persist the input or include any stderr/credentials in errors.
        with tempfile.TemporaryFile(mode='w+b') as output:
            process=subprocess.Popen([str(self.python),'-c',_CLIENT],stdin=subprocess.PIPE,
                stdout=output,stderr=subprocess.DEVNULL,
                creationflags=0x08000000 if os.name=='nt' else 0)
            try:
                process.communicate(json.dumps({'server':str(self.server),'history':history,
                    'task':task,'budget':1200},ensure_ascii=False).encode('utf-8'),timeout=self.timeout)
            except subprocess.TimeoutExpired as exc:
                if os.name=='nt':
                    subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],
                        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=2,
                        creationflags=0x08000000)
                process.kill();process.wait(timeout=1)
                raise TimeoutError('TOMC local deadline exceeded') from exc
            if process.returncode or output.tell()>100000:
                raise RuntimeError('TOMC local preparation unavailable')
            output.seek(0);value=json.loads(output.read(100001))
        if not isinstance(value.get('memory'),str):
            raise ValueError('TOMC did not return context')
        return value['memory']
