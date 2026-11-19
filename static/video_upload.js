// Subida de un video corto directo a Drive. El servidor solo abre la sesión.
const VIDEO_MAX_BYTES = 104857600;
const VIDEO_MAX_SECONDS = 60;
const VIDEO_DURATION_GRACE = 0.05;
const VIDEO_CHUNK = 8 * 1024 * 1024;
const MIME_BY_EXT = {mp4: "video/mp4", mov: "video/quicktime", webm: "video/webm"};
const VIDEO_ERRORS = {
  type: "Tiene que ser mp4, mov o webm",
  size: "Ese video pesa más de 100 MB. Probá con uno más corto.",
  duration: "Ese video dura más de 60 segundos. Recortalo y probalo de nuevo.",
  unreadable: "No pudimos leer la duración · probá con otro",
  screen: "Este video está en un formato que la pantalla no puede mostrar. En el iPhone andá a Ajustes > Cámara > Formatos, elegí «Más compatible» y grabalo de nuevo, o mandá otro video."
};
const UPLOAD_FAILED = "No se pudo subir el video · probá de nuevo";

function extensionOf(name) {
  const text = String(name || "");
  const dot = text.lastIndexOf(".");
  return dot >= 0 ? text.slice(dot + 1).toLowerCase() : "";
}

function videoRejection(file, duration) {
  const ext = extensionOf(file && file.name);
  if (!MIME_BY_EXT[ext]) return VIDEO_ERRORS.type;
  if (!file.size || file.size > VIDEO_MAX_BYTES) return VIDEO_ERRORS.size;
  if (typeof duration !== "number" || !isFinite(duration) || duration <= 0) return VIDEO_ERRORS.unreadable;
  if (duration > VIDEO_MAX_SECONDS + VIDEO_DURATION_GRACE) return VIDEO_ERRORS.duration;
  return null;
}

function u32(bytes, offset) {
  return ((bytes[offset] << 24) | (bytes[offset + 1] << 16) | (bytes[offset + 2] << 8) | bytes[offset + 3]) >>> 0;
}

function fourccAt(bytes, offset, text) {
  return bytes[offset] === text.charCodeAt(0)
    && bytes[offset + 1] === text.charCodeAt(1)
    && bytes[offset + 2] === text.charCodeAt(2)
    && bytes[offset + 3] === text.charCodeAt(3);
}

function hasFourcc(bytes, text) {
  for (let i = 0; i + 3 < bytes.length; i += 1) {
    if (fourccAt(bytes, i, text)) return true;
  }
  return false;
}

function boxHeader(header, remaining) {
  if (!header || header.length < 8 || remaining < 8) return null;
  let size = u32(header, 0);
  let headerLen = 8;
  if (size === 1) {
    if (header.length < 16 || remaining < 16) return null;
    const high = u32(header, 8);
    const low = u32(header, 12);
    size = high * 4294967296 + low;
    headerLen = 16;
  } else if (size === 0) {
    size = remaining;
  }
  if (size < headerLen || size > remaining) return null;
  const type = String.fromCharCode(header[4], header[5], header[6], header[7]);
  return {type, size};
}

async function readSlice(file, start, length) {
  const end = Math.min(file.size, start + length);
  if (!(file.slice)) return new Uint8Array();
  const slice = file.slice(start, end);
  const buffer = slice.arrayBuffer ? await slice.arrayBuffer() : null;
  return buffer ? new Uint8Array(buffer) : new Uint8Array();
}

async function containerIsHevc(file) {
  const size = file && file.size;
  if (!size || size < 8) return false;
  let offset = 0;
  for (let guard = 0; guard < 10000 && offset + 8 <= size; guard += 1) {
    const header = await readSlice(file, offset, Math.min(16, size - offset));
    const parsed = boxHeader(header, size - offset);
    if (!parsed) return false;
    if (parsed.type === "ftyp" || parsed.type === "moov") {
      const chunk = await readSlice(file, offset, Math.min(parsed.size, 2 * 1024 * 1024));
      if (hasFourcc(chunk, "hvc1") || hasFourcc(chunk, "hev1")) return true;
    }
    offset += parsed.size;
  }
  return false;
}

function defaultCanPlay(mime) {
  if (typeof document === "undefined" || !document.createElement) return "maybe";
  const el = document.createElement("video");
  if (!el.canPlayType) return "maybe";
  return el.canPlayType(mime) || "";
}

function defaultProbe(file) {
  return new Promise((resolve) => {
    if (typeof document === "undefined") {
      resolve(true);
      return;
    }
    const url = URL.createObjectURL(file);
    const el = document.createElement("video");
    let settled = false;
    const timer = setTimeout(() => finish(false), 8000);
    function finish(ok) {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      URL.revokeObjectURL(url);
      el.removeAttribute("src");
      try { el.load(); } catch (error) { /* ya no importa */ }
      resolve(ok);
    }
    el.preload = "metadata";
    el.muted = true;
    el.onloadedmetadata = () => finish(true);
    el.onerror = () => finish(false);
    el.src = url;
  });
}

async function screenFormatError(file, hooks) {
  hooks = hooks || {};
  if (await containerIsHevc(file)) return VIDEO_ERRORS.screen;
  const mime = MIME_BY_EXT[extensionOf(file && file.name)];
  if (!mime) return null;
  const canPlay = hooks.canPlay || defaultCanPlay;
  if (canPlay(mime, file) === "") return VIDEO_ERRORS.screen;
  const probe = hooks.probe || defaultProbe;
  if (await probe(file) === false) return VIDEO_ERRORS.screen;
  return null;
}

function chunkEnd(offset, total, chunkSize) {
  return Math.min(total - 1, offset + chunkSize - 1);
}

function resumeOffset(rangeHeader, fallback) {
  const match = /bytes=\s*(\d+)\s*-\s*(\d+)/i.exec(rangeHeader || "");
  if (!match) return fallback;
  const next = Number(match[2]) + 1;
  if (!isFinite(next) || next < 0) return fallback;
  return next;
}

function safeMessage(value, fallback) {
  if (typeof value !== "string") return fallback;
  const text = value.trim();
  if (!text || text.length > 180 || /[<>]/.test(text)) return fallback;
  return text;
}

function xhrPut(url, body, headers, onBytes) {
  return new Promise((resolve) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", url);
    Object.keys(headers || {}).forEach((key) => {
      if (headers[key] != null) xhr.setRequestHeader(key, headers[key]);
    });
    if (xhr.upload && onBytes) {
      xhr.upload.onprogress = (event) => {
        if (event.lengthComputable) onBytes(event.loaded);
      };
    }
    xhr.onload = () => resolve({
      status: xhr.status,
      range: xhr.getResponseHeader("Range"),
      text: xhr.responseText || ""
    });
    xhr.onerror = () => resolve({status: 0, range: null, text: ""});
    xhr.onabort = () => resolve({status: 0, range: null, text: ""});
    xhr.send(body == null ? null : body);
  });
}

function readDuration(file) {
  return new Promise((resolve, reject) => {
    if (typeof document === "undefined") {
      reject(new Error(VIDEO_ERRORS.unreadable));
      return;
    }
    const url = URL.createObjectURL(file);
    const el = document.createElement("video");
    let settled = false;
    const timer = setTimeout(() => finish(new Error(VIDEO_ERRORS.unreadable)), 8000);
    function finish(error, value) {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      el.onloadedmetadata = null;
      el.onerror = null;
      URL.revokeObjectURL(url);
      el.removeAttribute("src");
      try { el.load(); } catch (loadError) { /* el elemento ya no importa */ }
      if (error) reject(error);
      else resolve(value);
    }
    el.preload = "metadata";
    el.muted = true;
    el.playsInline = true;
    el.onloadedmetadata = () => {
      if (!isFinite(el.duration) || el.duration <= 0) finish(new Error(VIDEO_ERRORS.unreadable));
      else finish(null, el.duration);
    };
    el.onerror = () => finish(new Error(VIDEO_ERRORS.unreadable));
    el.src = url;
  });
}

async function sendChunk(url, file, offset, end, total, putImpl, sleep, onRetry, onBytes) {
  for (let attempt = 0; attempt < 4; attempt++) {
    const response = await putImpl(url, file.slice(offset, end + 1), {
      "Content-Range": `bytes ${offset}-${end}/${total}`
    }, onBytes);
    if (response.status === 200 || response.status === 201) return {done: true, offset: total};
    if (response.status === 308) return {done: false, offset: resumeOffset(response.range, end + 1)};
    if (response.status === 410 || response.status === 404) {
      const expired = new Error(UPLOAD_FAILED);
      expired.code = "restart";
      throw expired;
    }
    if (response.status !== 0 && response.status < 500) throw new Error(UPLOAD_FAILED);
    onRetry();
    await sleep(500 * (attempt + 1));
    const probe = await putImpl(url, null, {"Content-Range": `bytes */${total}`}, null);
    if (probe.status === 200 || probe.status === 201) return {done: true, offset: total};
    if (probe.status === 308 && probe.range) {
      const next = resumeOffset(probe.range, offset);
      if (next > offset) return {done: next >= total, offset: next};
    }
  }
  throw new Error(UPLOAD_FAILED);
}

function guestField(value, limit) {
  if (typeof value !== "string") return "";
  const text = value.trim();
  if (!text || /[<>]/.test(text)) return "";
  return text.slice(0, limit);
}

async function registerVideo(fetchImpl, uid, size, duration, sleep, hooks) {
  let last = UPLOAD_FAILED;
  const payload = {uid, size, duration};
  const nombre = guestField(hooks && hooks.nombre, 40);
  const saludo = guestField(hooks && hooks.saludo, 280);
  if (nombre) payload.nombre = nombre;
  if (saludo) payload.saludo = saludo;
  for (let attempt = 0; attempt < 3; attempt++) {
    if (attempt) await sleep(300 * attempt);
    let response;
    try {
      response = await fetchImpl("/api/videos/register", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(payload)
      });
    } catch (error) {
      continue;
    }
    const data = await response.json().catch(() => ({}));
    if (response.ok && data.ok) return;
    last = safeMessage(data.error, UPLOAD_FAILED);
    if (response.status !== 409 && response.status < 500) break;
  }
  throw new Error(last);
}

async function uploadOnce(file, duration, hooks) {
  const chunkSize = hooks.chunkSize || VIDEO_CHUNK;
  const fetchImpl = hooks.fetch || fetch;
  const putImpl = hooks.put || xhrPut;
  const sleep = hooks.sleep || (ms => new Promise(resolve => setTimeout(resolve, ms)));
  const onProgress = hooks.onProgress || function () {};
  const onRetry = hooks.onRetry || function () {};
  const rejection = videoRejection(file, duration);
  if (rejection) throw new Error(rejection);
  const ext = extensionOf(file.name);
  const sessionResponse = await fetchImpl("/api/videos/session", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ext, size: file.size, mime: MIME_BY_EXT[ext], duration})
  });
  const session = await sessionResponse.json().catch(() => ({}));
  if (!sessionResponse.ok || !session.ok || typeof session.uploadUrl !== "string" || !/^[0-9a-f]{32}$/.test(session.uid || "")) {
    throw new Error(safeMessage(session.error, UPLOAD_FAILED));
  }
  if (!/^https:\/\/www\.googleapis\.com\/upload\/drive\//.test(session.uploadUrl)) {
    throw new Error(UPLOAD_FAILED);
  }
  let offset = 0;
  let guard = 0;
  while (offset < file.size) {
    if (++guard > 10000) throw new Error(UPLOAD_FAILED);
    const end = chunkEnd(offset, file.size, chunkSize);
    const sent = await sendChunk(session.uploadUrl, file, offset, end, file.size, putImpl, sleep, onRetry, (loaded) => {
      onProgress(Math.min(file.size, offset + loaded), file.size);
    });
    if (sent.done || sent.offset >= file.size) {
      offset = file.size;
      break;
    }
    if (sent.offset <= offset) throw new Error(UPLOAD_FAILED);
    offset = sent.offset;
    onProgress(offset, file.size);
  }
  onProgress(file.size, file.size);
  await registerVideo(fetchImpl, session.uid, file.size, duration, sleep, hooks);
}

async function uploadVideoFile(file, duration, hooks) {
  hooks = hooks || {};
  try {
    await uploadOnce(file, duration, hooks);
  } catch (error) {
    if (!error || error.code !== "restart") throw error;
    if (hooks.onRetry) hooks.onRetry();
    await uploadOnce(file, duration, hooks);
  }
}

const videoUploadApi = {
  videoRejection, screenFormatError, containerIsHevc, uploadVideoFile, readDuration, chunkEnd, resumeOffset,
  VIDEO_MAX_BYTES, VIDEO_MAX_SECONDS, VIDEO_CHUNK, VIDEO_ERRORS, UPLOAD_FAILED, MIME_BY_EXT
};
if (typeof module !== "undefined" && module.exports) module.exports = videoUploadApi;
