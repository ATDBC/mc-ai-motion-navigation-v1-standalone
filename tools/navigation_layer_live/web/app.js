const canvas = document.querySelector('#viewport');
const gl = canvas.getContext('webgl2', {
  antialias: true,
  alpha: false,
  depth: true,
  powerPreference: 'high-performance',
});
const connection = document.querySelector('#connection');
const fields = Object.fromEntries(
  ['sequence', 'age', 'sample', 'cells', 'position', 'look', 'geometry', 'renderer']
    .map(id => [id, document.querySelector(`#${id}`)]),
);

if (!gl) {
  document.querySelector('#unsupported').hidden = false;
  throw new Error('WebGL2 is required');
}

const rendererInfo = gl.getExtension('WEBGL_debug_renderer_info');
fields.renderer.textContent = rendererInfo
  ? gl.getParameter(rendererInfo.UNMASKED_RENDERER_WEBGL)
  : gl.getParameter(gl.RENDERER);

const VERTEX_SHADER = `#version 300 es
precision highp float;
in vec3 aPosition;
in vec4 aColor;
uniform vec3 uCamera;
uniform vec3 uRight;
uniform vec3 uUp;
uniform vec3 uForward;
uniform float uTanHalfHorizontal;
uniform float uTanHalfVertical;
uniform float uNear;
uniform float uFar;
uniform float uPointSize;
out vec4 vColor;
void main() {
  vec3 relative = aPosition - uCamera;
  vec3 view = vec3(dot(relative, uRight), dot(relative, uUp), dot(relative, uForward));
  float a = (uFar + uNear) / (uFar - uNear);
  float b = (-2.0 * uFar * uNear) / (uFar - uNear);
  gl_Position = vec4(view.x / uTanHalfHorizontal, view.y / uTanHalfVertical,
                     a * view.z + b, view.z);
  gl_PointSize = uPointSize;
  vColor = aColor;
}`;

const FRAGMENT_SHADER = `#version 300 es
precision mediump float;
in vec4 vColor;
out vec4 color;
void main() { color = vColor; }`;

function shader(type, source) {
  const value = gl.createShader(type);
  gl.shaderSource(value, source);
  gl.compileShader(value);
  if (!gl.getShaderParameter(value, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(value));
  return value;
}

const program = gl.createProgram();
gl.attachShader(program, shader(gl.VERTEX_SHADER, VERTEX_SHADER));
gl.attachShader(program, shader(gl.FRAGMENT_SHADER, FRAGMENT_SHADER));
gl.linkProgram(program);
if (!gl.getProgramParameter(program, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(program));
gl.useProgram(program);

const attributePosition = gl.getAttribLocation(program, 'aPosition');
const attributeColor = gl.getAttribLocation(program, 'aColor');
const uniforms = Object.fromEntries([
  'uCamera', 'uRight', 'uUp', 'uForward', 'uTanHalfHorizontal',
  'uTanHalfVertical', 'uNear', 'uFar', 'uPointSize',
].map(name => [name, gl.getUniformLocation(program, name)]));

function batch() {
  const vao = gl.createVertexArray();
  const buffer = gl.createBuffer();
  gl.bindVertexArray(vao);
  gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
  gl.enableVertexAttribArray(attributePosition);
  gl.vertexAttribPointer(attributePosition, 3, gl.FLOAT, false, 28, 0);
  gl.enableVertexAttribArray(attributeColor);
  gl.vertexAttribPointer(attributeColor, 4, gl.FLOAT, false, 28, 12);
  gl.bindVertexArray(null);
  return {vao, buffer, count: 0};
}

const batches = {air: batch(), faces: batch(), edges: batch()};
let frame = null;
let receiptAt = 0;
let pendingRender = false;
let boxCount = 0;

function push(target, point, color) { target.push(...point, ...color); }

const FACE_INDICES = [
  0,1,2, 0,2,3, 4,6,5, 4,7,6,
  0,4,5, 0,5,1, 3,2,6, 3,6,7,
  0,3,7, 0,7,4, 1,5,6, 1,6,2,
];
const EDGE_INDICES = [0,1, 1,2, 2,3, 3,0, 4,5, 5,6, 6,7, 7,4, 0,4, 1,5, 2,6, 3,7];

function appendBox(faces, edges, x, y, z, box, faceColor, edgeColor) {
  const [x0, y0, z0, x1, y1, z1] = box;
  const points = [
    [x+x0,y+y0,z+z0], [x+x1,y+y0,z+z0], [x+x1,y+y1,z+z0], [x+x0,y+y1,z+z0],
    [x+x0,y+y0,z+z1], [x+x1,y+y0,z+z1], [x+x1,y+y1,z+z1], [x+x0,y+y1,z+z1],
  ];
  for (const index of FACE_INDICES) push(faces, points[index], faceColor);
  for (const index of EDGE_INDICES) push(edges, points[index], edgeColor);
  boxCount += 1;
}

function colors(entry) {
  if (entry.fluid) return {face: [0.10,0.58,1.0,0.20], edge: [0.30,0.78,1.0,0.72]};
  if (entry.category === 'occupied') return {face: [1.0,0.22,0.13,0.38], edge: [1.0,0.52,0.28,0.92]};
  return {face: [0.96,0.69,0.10,0.16], edge: [1.0,0.82,0.30,0.70]};
}

function upload(target, values) {
  gl.bindBuffer(gl.ARRAY_BUFFER, target.buffer);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(values), gl.DYNAMIC_DRAW);
  target.count = values.length / 7;
}

function rebuild(next) {
  const started = performance.now();
  const air = [], faces = [], edges = [];
  boxCount = 0;
  for (const [x, y, z0, z1, paletteIndex] of next.runs) {
    const entry = next.palette[paletteIndex];
    for (let z = z0; z < z1; z += 1) {
      if (entry.category === 'air') {
        push(air, [x+0.5,y+0.5,z+0.5], [0.12,0.55,0.94,0.13]);
        continue;
      }
      const shade = colors(entry);
      const geometry = entry.boxes.length ? entry.boxes : [[0,0,0,1,1,1]];
      for (const box of geometry) appendBox(faces, edges, x, y, z, box, shade.face, shade.edge);
    }
  }
  upload(batches.air, air);
  upload(batches.faces, faces);
  upload(batches.edges, edges);
  fields.geometry.textContent = `${boxCount} 体 / ${batches.air.count} 空气点 · ${(performance.now()-started).toFixed(1)} ms`;
}

function resize() {
  const scale = Math.min(window.devicePixelRatio || 1, 1.5);
  const width = Math.max(1, Math.floor(canvas.clientWidth * scale));
  const height = Math.max(1, Math.floor(canvas.clientHeight * scale));
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width;
    canvas.height = height;
    gl.viewport(0, 0, width, height);
  }
}

function scheduleRender() {
  if (pendingRender) return;
  pendingRender = true;
  requestAnimationFrame(draw);
}

function draw() {
  pendingRender = false;
  resize();
  gl.clearColor(0.012, 0.028, 0.055, 1);
  gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
  if (!frame) return;

  const yaw = frame.camera.yaw * Math.PI / 180;
  const pitch = frame.camera.pitch * Math.PI / 180;
  const cp = Math.cos(pitch);
  const forward = [-Math.sin(yaw)*cp, -Math.sin(pitch), Math.cos(yaw)*cp];
  // Minecraft yaw=0 faces +Z, so screen-right points toward -X.
  const right = [-Math.cos(yaw), 0, -Math.sin(yaw)];
  const up = [
    right[1]*forward[2] - right[2]*forward[1],
    right[2]*forward[0] - right[0]*forward[2],
    right[0]*forward[1] - right[1]*forward[0],
  ];
  const tanHorizontal = Math.tan(frame.horizontal_fov * Math.PI / 360);
  const aspect = canvas.width / canvas.height;
  gl.useProgram(program);
  gl.uniform3f(uniforms.uCamera, frame.camera.x, frame.camera.y, frame.camera.z);
  gl.uniform3fv(uniforms.uRight, right);
  gl.uniform3fv(uniforms.uUp, up);
  gl.uniform3fv(uniforms.uForward, forward);
  gl.uniform1f(uniforms.uTanHalfHorizontal, tanHorizontal);
  gl.uniform1f(uniforms.uTanHalfVertical, tanHorizontal / aspect);
  gl.uniform1f(uniforms.uNear, 0.04);
  gl.uniform1f(uniforms.uFar, frame.range + 2);
  gl.enable(gl.BLEND);
  gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);

  gl.disable(gl.DEPTH_TEST);
  gl.uniform1f(uniforms.uPointSize, Math.min(window.devicePixelRatio || 1, 1.5) * 2.0);
  gl.bindVertexArray(batches.air.vao);
  gl.drawArrays(gl.POINTS, 0, batches.air.count);

  gl.enable(gl.DEPTH_TEST);
  gl.depthMask(true);
  gl.uniform1f(uniforms.uPointSize, 1);
  gl.bindVertexArray(batches.faces.vao);
  gl.drawArrays(gl.TRIANGLES, 0, batches.faces.count);

  gl.disable(gl.DEPTH_TEST);
  gl.bindVertexArray(batches.edges.vao);
  gl.drawArrays(gl.LINES, 0, batches.edges.count);
  gl.bindVertexArray(null);
}

function updateStats(next) {
  fields.sequence.textContent = next.sequence;
  fields.sample.textContent = `${(next.sample_duration_ns / 1e6).toFixed(2)} ms`;
  fields.cells.textContent = `${next.queried_cells} / 未知 ${next.unknown_cells}`;
  fields.position.textContent = `${next.player.x.toFixed(2)}, ${next.player.y.toFixed(2)}, ${next.player.z.toFixed(2)}`;
  fields.look.textContent = `${next.camera.yaw.toFixed(1)}° / ${next.camera.pitch.toFixed(1)}°`;
}

const events = new EventSource('/events');
events.onmessage = event => {
  const next = JSON.parse(event.data);
  frame = next;
  receiptAt = performance.now();
  rebuild(next);
  updateStats(next);
  connection.textContent = '实时连接';
  connection.className = 'badge live';
  scheduleRender();
};
events.onerror = () => {
  connection.textContent = '连接中断，正在重连';
  connection.className = 'badge lost';
};

setInterval(() => {
  fields.age.textContent = receiptAt ? `${Math.max(0, performance.now()-receiptAt).toFixed(0)} ms` : '—';
}, 50);
window.addEventListener('resize', () => { resize(); scheduleRender(); });
resize();
