"""Runs INSIDE disconnected Docker test container; validates stdio MCP lifecycle."""
import asyncio
import json
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def probe():
    manifest=json.loads(Path('/workspace/mcp_manifest.json').read_text())
    argv=manifest['argv']
    params=StdioServerParameters(command=argv[0],args=argv[1:],env={
        'HOME':'/tmp','PYTHONDONTWRITEBYTECODE':'1','PYTHONPATH':'/workspace'})
    async with asyncio.timeout(25):
        async with stdio_client(params) as (reader,writer):
            async with ClientSession(reader,writer) as session:
                await session.initialize()
                response=await session.list_tools()
                listed={t.name:t for t in response.tools}
                required=manifest.get('expected_tools',[])
                if not required or not all(isinstance(x,str) and x for x in required):
                    raise ValueError('expected_tools must be nonempty')
                if not set(required).issubset(listed):
                    raise ValueError('Missing required MCP tool')
                for tool in response.tools:
                    if not isinstance(tool.inputSchema,dict):
                        raise ValueError('MCP tool schema invalid')
                calls=manifest.get('test_calls',[])
                if not calls: raise ValueError('At least one test_calls required')
                for call in calls:
                    if call['name'] not in listed: raise ValueError('Unknown tool call')
                    result=await session.call_tool(call['name'],call.get('arguments',{}))
                    if result.isError: raise ValueError('MCP tool returned isError')
    print('MCP initialize, list_tools, call_tool: OK')

asyncio.run(probe())
