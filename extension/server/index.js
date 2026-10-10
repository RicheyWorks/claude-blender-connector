#!/usr/bin/env node
// Blender connector for Claude: a dependency-free MCP server (stdio, JSON-RPC 2.0)
// that forwards tool calls to the "Claude Blender Bridge" add-on running inside Blender.
'use strict';
const net = require('net');
const readline = require('readline');

const HOST = process.env.BLENDER_HOST || '127.0.0.1';
const PORT = parseInt(process.env.BLENDER_PORT || '9876', 10) || 9876;
const SERVER_INFO = { name: 'blender', version: '1.0.1' };
const SUPPORTED = ['2025-06-18', '2025-03-26', '2024-11-05'];

const TOOLS = [
  {
    name: 'blender_status',
    description: 'Check that Blender is open and the Claude Blender Bridge add-on is listening. Returns the Blender version and the open .blend file. Call this first.',
    inputSchema: { type: 'object', properties: {}, additionalProperties: false },
    command: 'ping', timeout: 10000,
  },
  {
    name: 'get_scene_info',
    description: 'Summarize the current Blender scene: objects (name, type, location, collections), active object, selection, camera, render settings, frame range, collections and materials.',
    inputSchema: { type: 'object', properties: { limit: { type: 'integer', minimum: 1, maximum: 2000, description: 'Maximum objects to list (default 200)' } }, additionalProperties: false },
    command: 'get_scene_info', timeout: 30000,
  },
  {
    name: 'get_object_info',
    description: 'Details for one object by exact name: transform, dimensions, materials, modifiers, parenting, and mesh/camera/light/curve data.',
    inputSchema: { type: 'object', properties: { name: { type: 'string', description: 'Exact object name' } }, required: ['name'], additionalProperties: false },
    command: 'get_object_info', timeout: 30000,
  },
  {
    name: 'execute_python',
    description: 'Run Python inside the open Blender session on its main thread, with `bpy` already imported. This is how to build and change scenes: create geometry, materials, lights, cameras, modifiers, geometry nodes, animation. stdout is captured and returned; assign a JSON-serializable value to a variable named `result` to return data. Changes are live in the user\'s Blender and are not saved to disk unless the code saves.',
    inputSchema: { type: 'object', properties: { code: { type: 'string', description: 'Python source to execute' } }, required: ['code'], additionalProperties: false },
    command: 'execute_code', timeout: 600000,
  },
  {
    name: 'viewport_screenshot',
    description: 'Capture the 3D Viewport as the user sees it and return it as an image. Fast; use it to check work without rendering.',
    inputSchema: { type: 'object', properties: { max_size: { type: 'integer', minimum: 128, maximum: 4096, description: 'Longest edge of the returned image in pixels (default 1200)' } }, additionalProperties: false },
    command: 'viewport_screenshot', timeout: 60000,
  },
  {
    name: 'render_image',
    description: 'Render the current frame through the active camera with the scene\'s render settings, save it as a PNG and return a preview. Blender\'s interface is frozen while it renders. Requires an active camera.',
    inputSchema: {
      type: 'object',
      properties: {
        output_path: { type: 'string', description: 'Where to save the full-size PNG on the user\'s computer (default: a temp file)' },
        resolution_percentage: { type: 'integer', minimum: 1, maximum: 100, description: 'Render at this percent of scene resolution, for a quicker draft' },
        max_size: { type: 'integer', minimum: 128, maximum: 4096, description: 'Longest edge of the returned preview in pixels (default 1200)' },
      },
      additionalProperties: false,
    },
    command: 'render_image', timeout: 1800000,
  },
];

function callBlender(type, params, timeoutMs) {
  return new Promise((resolve, reject) => {
    const sock = net.createConnection({ host: HOST, port: PORT });
    const chunks = [];
    let settled = false;
    const finish = (err, val) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      sock.destroy();
      err ? reject(err) : resolve(val);
    };
    const timer = setTimeout(() => finish(new Error(`Blender did not answer within ${Math.round(timeoutMs / 1000)}s. It may be busy, rendering, or showing a dialog.`)), timeoutMs);
    sock.on('connect', () => sock.write(JSON.stringify({ id: 1, type, params: params || {} }) + '\n'));
    sock.on('data', (d) => {
      chunks.push(d);
      if (d.includes(10)) {
        const text = Buffer.concat(chunks).toString('utf8');
        const line = text.slice(0, text.indexOf('\n'));
        try { finish(null, JSON.parse(line)); } catch (e) { finish(new Error('Bad reply from Blender: ' + e.message)); }
      }
    });
    sock.on('error', (e) => {
      if (e.code === 'ECONNREFUSED' || e.code === 'ETIMEDOUT' || e.code === 'EHOSTUNREACH') {
        finish(new Error(`Could not reach Blender on ${HOST}:${PORT}. Open Blender, make sure the "Claude Blender Bridge" add-on is enabled, and check the Claude tab in the 3D Viewport sidebar (press N) says "Listening".`));
      } else finish(e);
    });
    sock.on('close', () => finish(new Error('Blender closed the connection before answering.')));
  });
}

async function runTool(name, args) {
  const tool = TOOLS.find((t) => t.name === name);
  if (!tool) return { isError: true, content: [{ type: 'text', text: `Unknown tool: ${name}` }] };
  let reply;
  try {
    reply = await callBlender(tool.command, args || {}, tool.timeout);
  } catch (e) {
    return { isError: true, content: [{ type: 'text', text: e.message }] };
  }
  if (!reply || reply.status !== 'ok') {
    const msg = (reply && reply.message) || 'Blender reported an error';
    const tb = reply && reply.traceback ? '\n\n' + reply.traceback : '';
    return { isError: true, content: [{ type: 'text', text: msg + tb }] };
  }
  const result = reply.result || {};
  const content = [];
  if (typeof result.image_png_base64 === 'string') {
    const { image_png_base64, ...meta } = result;
    content.push({ type: 'image', data: image_png_base64, mimeType: 'image/png' });
    content.push({ type: 'text', text: JSON.stringify(meta, null, 2) });
  } else {
    content.push({ type: 'text', text: JSON.stringify(result, null, 2) });
  }
  return { content };
}

function send(msg) { process.stdout.write(JSON.stringify(msg) + '\n'); }

async function handle(msg) {
  const { id, method, params } = msg;
  const isRequest = id !== undefined && id !== null;
  try {
    switch (method) {
      case 'initialize': {
        const asked = params && params.protocolVersion;
        return send({ jsonrpc: '2.0', id, result: {
          protocolVersion: SUPPORTED.includes(asked) ? asked : SUPPORTED[0],
          capabilities: { tools: {} },
          serverInfo: SERVER_INFO,
          instructions: 'Drives the user\'s open Blender session. Call blender_status first; build scenes with execute_python; check work with viewport_screenshot (fast) or render_image (final).',
        } });
      }
      case 'ping': return send({ jsonrpc: '2.0', id, result: {} });
      case 'tools/list':
        return send({ jsonrpc: '2.0', id, result: { tools: TOOLS.map(({ name, description, inputSchema }) => ({ name, description, inputSchema })) } });
      case 'tools/call': {
        const result = await runTool(params && params.name, params && params.arguments);
        return send({ jsonrpc: '2.0', id, result });
      }
      case 'resources/list': return send({ jsonrpc: '2.0', id, result: { resources: [] } });
      case 'prompts/list': return send({ jsonrpc: '2.0', id, result: { prompts: [] } });
      default:
        if (isRequest) send({ jsonrpc: '2.0', id, error: { code: -32601, message: `Method not found: ${method}` } });
    }
  } catch (e) {
    if (isRequest) send({ jsonrpc: '2.0', id, error: { code: -32603, message: String((e && e.message) || e) } });
  }
}

const rl = readline.createInterface({ input: process.stdin, terminal: false });
let pending = 0, closing = false;
rl.on('line', (line) => {
  line = line.trim();
  if (!line) return;
  let msg;
  try { msg = JSON.parse(line); } catch (e) {
    return send({ jsonrpc: '2.0', id: null, error: { code: -32700, message: 'Parse error' } });
  }
  for (const m of Array.isArray(msg) ? msg : [msg]) {
    pending++;
    handle(m).finally(() => { pending--; if (closing && pending === 0) process.exit(0); });
  }
});
rl.on('close', () => { closing = true; if (pending === 0) process.exit(0); });
process.on('uncaughtException', (e) => { process.stderr.write('[blender-connector] ' + (e && e.stack || e) + '\n'); });
