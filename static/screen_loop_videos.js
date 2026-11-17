// Videos de invitados en /loop. El loop de fotos no se toca: este archivo
// solo agrega un turno de video, en mudo, con el mismo marco que las fotos.
const VIDEO_CAP_S = 60;
const GOLD = "rgb(232, 196, 104)";

function borderPx(videoW, videoH) {
  const longer = Math.max(Number(videoW) || 0, Number(videoH) || 0);
  return Math.max(10, Math.trunc(longer * 0.012));
}

function frameBox(videoW, videoH, stageW, stageH) {
  const border = borderPx(videoW, videoH);
  const framedW = videoW + border * 2;
  const framedH = videoH + border * 2;
  const scale = Math.min(stageW / framedW, stageH / framedH);
  return {
    border,
    color: GOLD,
    scale,
    width: framedW * scale,
    height: framedH * scale,
    borderWidth: border * scale,
  };
}

function outputOptions(search) {
  const params = new URLSearchParams(search || "");
  return {
    audio: params.get("audio") === "1",
    marco: params.get("marco") === "1",
  };
}

function createTurnClock(opts) {
  const holdMs = opts.holdMs;
  const showNext = opts.showNext;
  const setInterval = opts.setInterval;
  const clearInterval = opts.clearInterval;
  const play = opts.play;
  let timer = null;
  let busy = false;
  let videos = [];
  let cursor = 0;
  let photoTicks = 0;
  const skipped = new Set();

  function playable() {
    return videos.filter((video) => video && video.uid && !skipped.has(video.uid));
  }

  function videoDue() {
    const list = playable();
    if (!list.length) return null;
    photoTicks += 1;
    if (photoTicks % 2 !== 0) return null;
    const video = list[cursor % list.length];
    cursor += 1;
    return video;
  }

  function arm() {
    if (timer != null) clearInterval(timer);
    timer = setInterval(tick, holdMs);
    return timer;
  }

  function tick() {
    if (busy) return;
    const video = videoDue();
    if (!video) {
      showNext();
      return;
    }
    busy = true;
    if (timer != null) clearInterval(timer);
    timer = null;
    play(video, (reason) => {
      if (reason === "decode") skipped.add(video.uid);
      busy = false;
      showNext();
      arm();
    });
  }

  return {
    arm,
    tick,
    setVideos(list) {
      videos = Array.isArray(list) ? list.slice() : [];
    },
  };
}

function applyFrame(frame, video, stage) {
  const box = frameBox(video.videoWidth, video.videoHeight, stage.clientWidth, stage.clientHeight);
  if (!box.scale || !isFinite(box.scale)) return;
  frame.style.width = box.width + "px";
  frame.style.height = box.height + "px";
  frame.style.borderWidth = box.borderWidth + "px";
  frame.style.borderColor = box.color;
}

function playClip(doc, video, done) {
  const clip = doc.getElementById("clip");
  const frame = doc.getElementById("clipFrame");
  const stage = doc.getElementById("stage");
  const emptyMsg = doc.getElementById("empty");
  if (!clip || !frame || !stage) {
    done("stall");
    return;
  }
  clip.onended = null;
  clip.onerror = null;
  clip.ontimeupdate = null;
  clip.onplaying = null;
  clip.onloadedmetadata = null;
  clip.pause();
  while (clip.firstChild) clip.removeChild(clip.firstChild);
  clip.removeAttribute("src");
  try { clip.load(); } catch (error) { /* el corte anterior ya soltó el video */ }
  const token = (clip._token || 0) + 1;
  clip._token = token;
  setTimeout(() => {
    if (clip._token !== token) return;
    clip.muted = true;
    frame.classList.add("playing");
    if (emptyMsg) emptyMsg.style.display = "none";
    const source = doc.createElement("source");
    source.src = video.src;
    source.type = video.mime;
    clip.appendChild(source);
    let watchdog = setTimeout(() => finish("stall"), 45000);
    function finish(reason) {
      if (clip._token !== token) return;
      clip._token += 1;
      clearTimeout(watchdog);
      clip.onended = null;
      clip.onerror = null;
      clip.ontimeupdate = null;
      clip.onplaying = null;
      clip.onloadedmetadata = null;
      clip.pause();
      while (clip.firstChild) clip.removeChild(clip.firstChild);
      clip.removeAttribute("src");
      try { clip.load(); } catch (error) { /* ya no hay video */ }
      frame.classList.remove("playing");
      if (emptyMsg && !stage.querySelector(".slide")) emptyMsg.style.display = "flex";
      done(reason);
    }
    function fit() {
      if (clip._token !== token) return;
      if (clip.videoWidth && clip.videoHeight) applyFrame(frame, clip, stage);
    }
    clip.onloadedmetadata = fit;
    clip.onended = () => finish("ended");
    clip.onerror = () => {
      const code = clip.error ? clip.error.code : 0;
      finish(code === 3 || code === 4 ? "decode" : "stall");
    };
    clip.ontimeupdate = () => {
      if (clip.currentTime >= VIDEO_CAP_S) finish("cap");
    };
    clip.onplaying = () => clearTimeout(watchdog);
    if (!clip._fitBound) {
      clip._fitBound = true;
      window.addEventListener("resize", () => {
        if (frame.classList.contains("playing")) fit();
      });
    }
    clip.load();
    const pending = clip.play();
    if (pending && pending.catch) pending.catch(() => finish("stall"));
  }, 0);
}

function validLoopVideo(item) {
  return item
    && typeof item.uid === "string"
    && /^[0-9a-f]{32}$/.test(item.uid)
    && item.src === "/videos/" + item.uid
    && typeof item.mime === "string"
    && item.playable !== false;
}

function attachLoop(doc, win) {
  const nativeSetInterval = win.setInterval.bind(win);
  const nativeClearInterval = win.clearInterval.bind(win);
  let clock = null;
  let pending = [];
  function publish(list) {
    pending = list;
    if (clock) clock.setVideos(pending);
  }
  win.setInterval = function (fn, ms) {
    if (!clock && typeof fn === "function" && fn.name === "showNext" && ms === 25000) {
      clock = createTurnClock({
        holdMs: ms,
        showNext: fn,
        setInterval: nativeSetInterval,
        clearInterval: nativeClearInterval,
        play: (video, done) => playClip(doc, video, done),
      });
      clock.setVideos(pending);
      return clock.arm();
    }
    return nativeSetInterval(fn, ms);
  };
  async function refreshVideos() {
    try {
      const res = await fetch("/api/videos", { cache: "no-store" });
      if (!res.ok) return;
      const data = await res.json();
      if (!Array.isArray(data.videos)) return;
      const list = data.videos.map((row) => ({
        uid: row && row.uid,
        src: row && row.uid ? "/videos/" + row.uid : "",
        mime: row && row.mime,
        ts: row && row.ts,
        playable: row && row.playable,
      })).filter(validLoopVideo).sort((a, b) => (a.ts || 0) - (b.ts || 0));
      publish(list);
    } catch (error) { /* la pantalla sigue con las fotos */ }
  }
  nativeSetInterval(refreshVideos, 8000);
  refreshVideos();
}

const screenLoopApi = {
  VIDEO_CAP_S,
  GOLD,
  borderPx,
  frameBox,
  outputOptions,
  createTurnClock,
  validLoopVideo,
};
if (typeof module !== "undefined" && module.exports) module.exports = screenLoopApi;
if (typeof window !== "undefined") window.screenLoopVideos = screenLoopApi;
if (typeof document !== "undefined" && document.getElementById && document.getElementById("clip")) {
  attachLoop(document, window);
}
