from mcp.server.fastmcp import FastMCP
mcp=FastMCP('integration-test')
@mcp.tool()
def echo(message: str) -> str:
    """Echo a test string without network access."""
    return message
if __name__=='__main__':
    mcp.run(transport='stdio')
