import asyncio
from asgiref.sync import async_to_sync
from mcp.client.sse import sse_client
from mcp.client.session import ClientSession
import json

class MCPClient:
    def __init__(self, mcp_server_url):
        self.url = mcp_server_url

    async def _list_tools_async(self):
        try:
            # O SDK Python do MCP ainda não suporta injeção de custom headers facilmente no sse_client
            # Se o servidor requerer o token no header e não puder ser passado em outro lugar,
            # talvez seja necessário estender o sse_client, mas para a v1 (e baseado na doc), vamos tentar a conexão pura.
            async with sse_client(self.url) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools_result = await session.list_tools()
                    
                    formatted_tools = []
                    for tool in tools_result.tools:
                        formatted_tools.append({
                            "name": tool.name,
                            "description": tool.description,
                            "parameters": tool.inputSchema
                        })
                    return formatted_tools
        except Exception as e:
            print(f"[MCP Error] Failed to list tools from {self.url}: {e}")
            return []

    async def _call_tool_async(self, tool_name, arguments):
        try:
            async with sse_client(self.url) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    mcp_result = await session.call_tool(tool_name, arguments=arguments)
                    
                    if mcp_result.content and len(mcp_result.content) > 0:
                        return mcp_result.content[0].text
                    return "Sucesso, mas sem retorno textual do MCP."
        except Exception as e:
            print(f"[MCP Error] Failed to call tool {tool_name}: {e}")
            return json.dumps({"error": str(e)})

    def get_tools_sync(self):
        """Versão síncrona para ser usada nas views do Django."""
        return async_to_sync(self._list_tools_async)()

    def call_tool_sync(self, tool_name, arguments):
        """Versão síncrona para ser usada nas views do Django."""
        return async_to_sync(self._call_tool_async)(tool_name, arguments)