import WaveSurfer from "./vendor/wavesurfer.esm.js";
import RegionsPlugin from "./vendor/regions.esm.js";
import TimelinePlugin from "./vendor/timeline.esm.js";

const $ = (selector) => document.querySelector(selector);
const elements = {
  title: $("#run-title"),
  mediaName: $("#media-name"),
  saveStatus: $("#save-status"),
  saveButton: $("#save-button"),
  video: $("#video"),
  audio: $("#audio"),
  mediaEmpty: $("#media-empty"),
  overlay: $("#subtitle-overlay"),
  previous: $("#previous-button"),
  replay: $("#replay-button"),
  next: $("#next-button"),
  speed: $("#speed-select"),
  loop: $("#loop-toggle"),
  timeReadout: $("#time-readout"),
  search: $("#search-input"),
  unresolvedOnly: $("#unresolved-toggle"),
  cueCount: $("#cue-count"),
  cueList: $("#cue-list"),
  cueNumber: $("#cue-number"),
  cueState: $("#cue-state"),
  confirm: $("#confirm-button"),
  cueText: $("#cue-text"),
  cueStart: $("#cue-start"),
  cueEnd: $("#cue-end"),
  validation: $("#validation-message"),
  zoom: $("#zoom-slider"),
  toast: $("#toast"),
};

const state = {
  payload: null,
  cues: [],
  audit: [],
  media: null,
  wavesurfer: null,
  regions: null,
  regionByCue: new Map(),
  rowByCue: new Map(),
  activeIndex: -1,
  dirty: false,
  confirmedAuditIds: new Set(),
  toastTimer: null,
  selectingFromPlayback: false,
};

function basePath() {
  return location.pathname.replace(/\/$/, "");
}

function formatClock(seconds, milliseconds = true) {
  const value = Math.max(0, Number(seconds) || 0);
  const hours = Math.floor(value / 3600);
  const minutes = Math.floor((value % 3600) / 60);
  const secs = Math.floor(value % 60);
  const millis = Math.round((value - Math.floor(value)) * 1000);
  const prefix = hours ? `${String(hours).padStart(2, "0")}:` : "";
  return `${prefix}${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}${milliseconds ? `.${String(millis).padStart(3, "0")}` : ""}`;
}

function showToast(message) {
  elements.toast.textContent = message;
  elements.toast.classList.add("visible");
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => elements.toast.classList.remove("visible"), 2400);
}

function setSaveState(kind, message) {
  elements.saveStatus.className = `save-status ${kind || ""}`.trim();
  elements.saveStatus.textContent = message;
}

function markDirty() {
  state.dirty = true;
  elements.saveButton.disabled = false;
  setSaveState("dirty", "有未保存修改");
}

function activeCue() {
  return state.activeIndex >= 0 ? state.cues[state.activeIndex] : null;
}

function auditForCue(cueId) {
  return state.audit.filter((item) => item.unresolved && item.cueIds.includes(cueId));
}

function isCuePendingConfirmation(cueId) {
  const rows = auditForCue(cueId);
  return rows.length > 0 && rows.every((row) => state.confirmedAuditIds.has(row.id));
}

function cueColor(cue) {
  if (isCuePendingConfirmation(cue.id)) return "rgba(132, 199, 132, 0.3)";
  if (cue.unresolved) return "rgba(242, 184, 75, 0.34)";
  return "rgba(84, 198, 194, 0.2)";
}

function updateCuePresentation(cue) {
  const row = state.rowByCue.get(cue.id);
  if (row) {
    row.querySelector(".cue-time").textContent = formatClock(cue.start, false);
    row.querySelector(".cue-copy").textContent = cue.text.replace(/\n/g, " ");
    row.classList.toggle("unresolved", cue.unresolved && !isCuePendingConfirmation(cue.id));
  }
  const region = state.regionByCue.get(cue.id);
  if (region) {
    region.setOptions({
      start: cue.start,
      end: cue.end,
      color: cueColor(cue),
    });
  }
  if (activeCue()?.id === cue.id) {
    elements.overlay.textContent = cue.text;
  }
}

function renderCueList() {
  const query = elements.search.value.trim().toLocaleLowerCase();
  const unresolvedOnly = elements.unresolvedOnly.checked;
  const fragment = document.createDocumentFragment();
  state.rowByCue.clear();
  let visible = 0;
  state.cues.forEach((cue, index) => {
    const matchesText = !query || cue.text.toLocaleLowerCase().includes(query);
    const matchesState = !unresolvedOnly || cue.unresolved;
    if (!matchesText || !matchesState) return;
    visible += 1;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "cue-row";
    button.setAttribute("role", "option");
    button.dataset.cueId = String(cue.id);
    button.classList.toggle("active", index === state.activeIndex);
    button.classList.toggle("unresolved", cue.unresolved && !isCuePendingConfirmation(cue.id));
    const time = document.createElement("span");
    time.className = "cue-time";
    time.textContent = formatClock(cue.start, false);
    const copy = document.createElement("span");
    copy.className = "cue-copy";
    copy.textContent = cue.text.replace(/\n/g, " ");
    button.append(time, copy);
    button.addEventListener("click", () => selectCue(index, { seek: true, play: false }));
    state.rowByCue.set(cue.id, button);
    fragment.append(button);
  });
  elements.cueList.replaceChildren(fragment);
  elements.cueCount.textContent = `${visible} / ${state.cues.length}`;
}

function selectCue(index, { seek = false, play = false, fromPlayback = false } = {}) {
  if (index < 0 || index >= state.cues.length) return;
  const previous = state.rowByCue.get(activeCue()?.id);
  if (previous) previous.classList.remove("active");
  state.activeIndex = index;
  const cue = activeCue();
  const current = state.rowByCue.get(cue.id);
  if (current) {
    current.classList.add("active");
    current.setAttribute("aria-selected", "true");
    if (!fromPlayback) current.scrollIntoView({ block: "nearest" });
  }
  elements.cueNumber.textContent = `字幕 ${cue.id}`;
  elements.cueText.disabled = false;
  elements.cueStart.disabled = false;
  elements.cueEnd.disabled = false;
  elements.cueText.value = cue.text;
  elements.cueStart.value = cue.start.toFixed(3);
  elements.cueEnd.value = cue.end.toFixed(3);
  elements.overlay.textContent = cue.text;
  const pending = isCuePendingConfirmation(cue.id);
  elements.cueState.textContent = cue.unresolved
    ? pending
      ? "待保存确认"
      : "待确认"
    : "";
  elements.confirm.disabled = !cue.unresolved;
  elements.confirm.classList.toggle("pending", pending);
  elements.confirm.textContent = pending ? "已标记确认" : "已听清并确认";
  elements.validation.textContent = "";
  if (seek) {
    state.media.currentTime = cue.start;
    if (play) state.media.play();
  }
}

function findCueAt(time) {
  let low = 0;
  let high = state.cues.length - 1;
  while (low <= high) {
    const middle = Math.floor((low + high) / 2);
    const cue = state.cues[middle];
    if (time < cue.start) high = middle - 1;
    else if (time >= cue.end) low = middle + 1;
    else return middle;
  }
  return -1;
}

function validateCueEdit(cue, index) {
  const previous = state.cues[index - 1];
  const next = state.cues[index + 1];
  if (!cue.text.trim()) return "字幕文字不能为空";
  if (!(cue.start >= 0 && cue.end > cue.start)) return "结束时间必须晚于开始时间";
  if (previous && cue.start + 0.05 < previous.end) return "开始时间与上一条字幕重叠";
  if (next && cue.end > next.start + 0.05) return "结束时间与下一条字幕重叠";
  if (cue.end > state.payload.duration + 0.05) return "结束时间超出媒体时长";
  return "";
}

function applyEditorChange() {
  const cue = activeCue();
  if (!cue) return;
  const candidate = {
    ...cue,
    text: elements.cueText.value.trim(),
    start: Number(elements.cueStart.value),
    end: Number(elements.cueEnd.value),
  };
  const message = validateCueEdit(candidate, state.activeIndex);
  elements.validation.textContent = message;
  if (message) return;
  cue.text = candidate.text;
  cue.start = Math.round(candidate.start * 1000) / 1000;
  cue.end = Math.round(candidate.end * 1000) / 1000;
  updateCuePresentation(cue);
  markDirty();
}

function confirmActiveAudit() {
  const cue = activeCue();
  if (!cue?.unresolved) return;
  const rows = auditForCue(cue.id);
  rows.forEach((row) => state.confirmedAuditIds.add(row.id));
  elements.confirm.classList.add("pending");
  elements.confirm.textContent = "已标记确认";
  elements.cueState.textContent = "待保存确认";
  updateCuePresentation(cue);
  markDirty();
}

async function saveChanges() {
  if (!state.dirty) return;
  applyEditorChange();
  if (elements.validation.textContent) return;
  elements.saveButton.disabled = true;
  setSaveState("", "正在保存…");
  try {
    const response = await fetch(`${basePath()}/api/save`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        revision: state.payload.revision,
        cues: state.cues.map(({ id, start, end, text }) => ({ id, start, end, text })),
        confirmedAuditIds: [...state.confirmedAuditIds],
      }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "保存失败");
    state.payload.revision = result.revision;
    state.dirty = false;
    state.confirmedAuditIds.forEach((id) => {
      const row = state.audit.find((item) => item.id === id);
      if (row) {
        row.unresolved = false;
        row.cueIds.forEach((cueId) => {
          const cue = state.cues.find((item) => item.id === cueId);
          if (cue) cue.unresolved = auditForCue(cueId).length > 0;
        });
      }
    });
    state.confirmedAuditIds.clear();
    renderCueList();
    selectCue(state.activeIndex);
    setSaveState("saved", "已保存");
    showToast(`已保存 ${result.changedCueCount} 条修改`);
  } catch (error) {
    elements.saveButton.disabled = false;
    setSaveState("error", "保存失败");
    showToast(error.message);
  }
}

async function loadWaveform() {
  const response = await fetch(`${basePath()}/waveform`);
  if (!response.ok) throw new Error("波形数据加载失败");
  const bytes = new Uint8Array(await response.arrayBuffer());
  const peaks = Float32Array.from(bytes, (value) => value / 255);
  const regions = RegionsPlugin.create();
  const timeline = TimelinePlugin.create({
    container: "#timeline",
    height: 22,
    timeInterval: 1,
    primaryLabelInterval: 5,
    secondaryLabelInterval: 1,
    style: {
      color: "#a8b0b4",
      fontSize: "10px",
    },
  });
  state.wavesurfer = WaveSurfer.create({
    container: "#waveform",
    media: state.media,
    peaks: [peaks],
    duration: state.payload.duration,
    waveColor: "#52616a",
    progressColor: "#54c6c2",
    cursorColor: "#f4f5f2",
    cursorWidth: 1,
    height: 124,
    minPxPerSec: Number(elements.zoom.value),
    dragToSeek: true,
    hideScrollbar: false,
    autoScroll: true,
    autoCenter: true,
    plugins: [regions, timeline],
  });
  state.regions = regions;
  state.cues.forEach((cue, index) => {
    const region = regions.addRegion({
      id: `cue-${cue.id}`,
      start: cue.start,
      end: cue.end,
      color: cueColor(cue),
      drag: true,
      resize: true,
      content: "",
    });
    state.regionByCue.set(cue.id, region);
    region.on("click", (event) => {
      event.stopPropagation();
      selectCue(index, { seek: true });
    });
  });
  regions.on("region-updated", (region) => {
    const cueId = Number(region.id.replace("cue-", ""));
    const index = state.cues.findIndex((cue) => cue.id === cueId);
    if (index < 0) return;
    const cue = state.cues[index];
    const previous = state.cues[index - 1];
    const next = state.cues[index + 1];
    const start = Math.max(previous?.end || 0, region.start);
    const end = Math.min(next?.start || state.payload.duration, region.end);
    if (end - start < 0.05) {
      region.setOptions({ start: cue.start, end: cue.end });
      showToast("字幕区间不能与相邻字幕重叠");
      return;
    }
    cue.start = Math.round(start * 1000) / 1000;
    cue.end = Math.round(end * 1000) / 1000;
    if (activeCue()?.id === cue.id) {
      elements.cueStart.value = cue.start.toFixed(3);
      elements.cueEnd.value = cue.end.toFixed(3);
    }
    updateCuePresentation(cue);
    markDirty();
  });
}

function bindEvents() {
  elements.search.addEventListener("input", renderCueList);
  elements.unresolvedOnly.addEventListener("change", renderCueList);
  elements.cueText.addEventListener("input", applyEditorChange);
  elements.cueStart.addEventListener("change", applyEditorChange);
  elements.cueEnd.addEventListener("change", applyEditorChange);
  elements.confirm.addEventListener("click", confirmActiveAudit);
  elements.saveButton.addEventListener("click", saveChanges);
  elements.previous.addEventListener("click", () => selectCue(state.activeIndex - 1, { seek: true }));
  elements.next.addEventListener("click", () => selectCue(state.activeIndex + 1, { seek: true }));
  elements.replay.addEventListener("click", () => {
    const cue = activeCue();
    if (!cue) return;
    state.media.currentTime = cue.start;
    state.media.play();
  });
  elements.speed.addEventListener("change", () => {
    state.media.playbackRate = Number(elements.speed.value);
  });
  elements.zoom.addEventListener("input", () => {
    state.wavesurfer?.zoom(Number(elements.zoom.value));
  });
  state.media.addEventListener("timeupdate", () => {
    const time = state.media.currentTime;
    const index = findCueAt(time);
    if (index >= 0 && index !== state.activeIndex) {
      selectCue(index, { fromPlayback: true });
    } else if (index < 0) {
      elements.overlay.textContent = "";
    }
    const cue = activeCue();
    if (elements.loop.checked && cue && time >= cue.end - 0.025) {
      state.media.currentTime = cue.start;
      state.media.play();
    }
    elements.timeReadout.textContent = `${formatClock(time)} / ${formatClock(state.payload.duration)}`;
  });
  document.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
      event.preventDefault();
      saveChanges();
    }
  });
  window.addEventListener("beforeunload", (event) => {
    if (!state.dirty) return;
    event.preventDefault();
    event.returnValue = "";
  });
}

async function initialize() {
  try {
    const response = await fetch(`${basePath()}/api/session`);
    if (!response.ok) throw new Error("无法读取转录结果");
    state.payload = await response.json();
    state.cues = state.payload.cues;
    state.audit = state.payload.audit;
    elements.title.textContent = state.payload.title;
    elements.mediaName.textContent = `${state.payload.media.name} · ${state.payload.media.source === "input" ? "原视频" : "标准化音频"}`;
    document.title = `${state.payload.title} · 字幕校订`;
    const isAudio = state.payload.media.kind === "audio";
    state.media = isAudio ? elements.audio : elements.video;
    elements.video.hidden = isAudio;
    elements.audio.hidden = !isAudio;
    elements.mediaEmpty.hidden = !isAudio;
    state.media.src = `${basePath()}/media`;
    elements.timeReadout.textContent = `00:00.000 / ${formatClock(state.payload.duration)}`;
    renderCueList();
    selectCue(0);
    bindEvents();
    await loadWaveform();
    elements.saveButton.disabled = true;
    setSaveState("", `已载入 ${state.cues.length} 条字幕`);
  } catch (error) {
    setSaveState("error", "载入失败");
    showToast(error.message);
  }
}

initialize();
