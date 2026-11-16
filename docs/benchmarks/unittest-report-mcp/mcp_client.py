"""Invoke the dedicated tool using actual MCP stdio messages; retain a wire receipt."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


def call_tool(manifest: str, output: str) -> dict:
    server = Path(__file__).with_name('report_tool.py')
    env = dict(os.environ, PYTHONIOENCODING='utf-8')
    messages = [
        {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
            'protocolVersion': '2025-11-25', 'capabilities': {}, 'clientInfo': {'name': 'benchmark-client', 'version': '1.0.0'}}},
        {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
        {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
        {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {
            'name': 'generate_unittest_report', 'arguments': {'manifest_path': manifest, 'output_dir': output}}},
    ]
    # Each exchange completes before the next; no assumed initialization or tool result.
    transcript = []
    with subprocess.Popen([sys.executable, '-X', 'utf8', str(server), '--stdio'],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, encoding='utf-8', env=env) as process:
        try:
            for message in messages:
                process.stdin.write(json.dumps(message) + '\n')
                process.stdin.flush()
                transcript.append({'direction': 'request', 'message': message})
                if 'id' not in message:
                    continue
                response = json.loads(process.stdout.readline())
                transcript.append({'direction': 'response', 'message': response})
                if response.get('id') != message['id'] or 'error' in response:
                    raise RuntimeError(response)
                if message['id'] == 1:
                    assert response['result']['protocolVersion'] == '2025-11-25'
                elif message['id'] == 2:
                    assert response['result']['tools'][0]['name'] == 'generate_unittest_report'
                else:
                    result = response['result']
            if result.get('isError'):
                raise RuntimeError(result)
            process.stdin.close()
            process.wait(timeout=10)
            if process.returncode:
                raise RuntimeError(process.stderr.read())
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
    target = Path(output) / 'mcp-transcript.json'
    target.write_text(json.dumps(transcript, ensure_ascii=False, indent=2), encoding='utf-8')
    return result['structuredContent']


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    print(json.dumps(call_tool(args.manifest, args.output), ensure_ascii=False))
