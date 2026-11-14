/* Real LiveKit media only. Tokens stay in memory and never enter URLs/storage. */
(() => {
  "use strict";
  const panel = document.getElementById("video-call");
  if (!panel) return;
  const status = document.getElementById("video-status");
  const local = document.getElementById("local-media");
  const remote = document.getElementById("remote-media");
  const start = document.getElementById("start-video");
  const join = document.getElementById("join-video");
  const end = document.getElementById("end-video");
  const microphone = document.getElementById("toggle-microphone");
  const camera = document.getElementById("toggle-camera");
  const csrf = document.querySelector("#video-csrf input[name=csrfmiddlewaretoken]").value;
  let room = null;
  let connecting = false;
  let callVersion = null;
  let closing = false;
  let polling = false;

  function message(text) { status.textContent = text; }
  function controls(connected) {
    join.disabled = connected || connecting;
    if (start) start.disabled = connected || connecting;
    microphone.disabled = !connected;
    camera.disabled = !connected;
  }
  function stopLocal() {
    if (!room) return;
    room.localParticipant.trackPublications.forEach((publication) => {
      if (publication.track) publication.track.stop();
    });
  }
  async function disconnect(text) {
    if (closing) return;
    closing = true;
    const oldRoom = room;
    stopLocal();
    room = null;
    callVersion = null;
    if (oldRoom) await oldRoom.disconnect();
    local.replaceChildren();
    remote.replaceChildren();
    connecting = false;
    controls(false);
    if (text) message(text);
    closing = false;
  }
  async function request(url, method = "POST") {
    const response = await fetch(url, {
      method, credentials: "same-origin", cache: "no-store", redirect: "error",
      headers: method === "POST" ? {"X-CSRFToken": csrf, "Accept": "application/json"} : {"Accept": "application/json"},
      signal: AbortSignal.timeout(8000),
    });
    if (!response.ok) {
      if (response.status === 401 || response.status === 403) {
        await disconnect("Access to this session has ended. Return to the consultation.");
        throw new Error("Access to this session has ended.");
      }
      let detail = "The video service is unavailable. You can continue in writing.";
      try { const body = await response.json(); if (body.error) detail = body.error; } catch { /* Fixed fallback. */ }
      throw new Error(detail);
    }
    return response.json();
  }
  function attach(track, container) {
    const element = track.attach();
    element.autoplay = true;
    element.playsInline = true;
    if (container === local) element.muted = true;
    container.appendChild(element);
  }
  async function connect(shouldStart) {
    if (connecting || room) return;
    if (!window.LivekitClient) { message("The video client is unavailable. You can continue in writing."); return; }
    connecting = true;
    controls(false);
    try {
      if (shouldStart) await request(panel.dataset.startUrl);
      const authorization = await request(panel.dataset.tokenUrl);
      callVersion = authorization.version;
      room = new window.LivekitClient.Room({adaptiveStream: true, dynacast: true});
      room.on(window.LivekitClient.RoomEvent.TrackSubscribed, (track) => attach(track, remote));
      room.on(window.LivekitClient.RoomEvent.TrackUnsubscribed, (track) => track.detach().forEach((element) => element.remove()));
      room.on(window.LivekitClient.RoomEvent.Disconnected, () => {
        if (!closing) void disconnect("Disconnected from video. You can continue in writing.");
      });
      room.on(window.LivekitClient.RoomEvent.Reconnecting, () => message("Video connection interrupted. Reconnecting…"));
      room.on(window.LivekitClient.RoomEvent.Reconnected, () => message("Connected. Verify the other participant before discussing care."));
      await room.connect(authorization.url, authorization.token);
      // Ask for devices only after server authorization and provider connection.
      await room.localParticipant.setCameraEnabled(true);
      await room.localParticipant.setMicrophoneEnabled(true);
      room.localParticipant.trackPublications.forEach((publication) => {
        if (publication.track && publication.kind === "video") attach(publication.track, local);
      });
      connecting = false;
      controls(true);
      microphone.textContent = "Mute microphone";
      camera.textContent = "Turn camera off";
      message("Connected. Verify the other participant before discussing care.");
    } catch (error) {
      await disconnect(error.message || "Unable to join video. You can continue in writing.");
    }
  }
  join.addEventListener("click", () => void connect(false));
  if (start) start.addEventListener("click", () => void connect(true));
  end.addEventListener("click", async () => {
    // Stop devices immediately, even if the provider cannot be reached.
    await disconnect("Ending the session…");
    try {
      const result = await request(panel.dataset.endUrl);
      message(result.cleanup === "DONE" ? "Session ended for both participants." : "Session ended. Provider cleanup is awaiting a retry.");
    } catch (error) { message(error.message); }
  });
  microphone.addEventListener("click", async () => {
    if (!room) return;
    try {
      const enabled = !room.localParticipant.isMicrophoneEnabled;
      await room.localParticipant.setMicrophoneEnabled(enabled);
      microphone.textContent = enabled ? "Mute microphone" : "Unmute microphone";
    } catch { message("Microphone permission or device is unavailable."); }
  });
  camera.addEventListener("click", async () => {
    if (!room) return;
    try {
      const enabled = !room.localParticipant.isCameraEnabled;
      await room.localParticipant.setCameraEnabled(enabled);
      local.replaceChildren();
      if (enabled) room.localParticipant.trackPublications.forEach((publication) => {
        if (publication.track && publication.kind === "video") attach(publication.track, local);
      });
      camera.textContent = enabled ? "Turn camera off" : "Turn camera on";
    } catch { message("Camera permission or device is unavailable."); }
  });
  const poll = window.setInterval(async () => {
    if (polling || closing) return;
    polling = true;
    try {
      const state = await request(panel.dataset.statusUrl, "GET");
      if (room && (!state.active || state.version !== callVersion)) {
        await disconnect("This video session has ended.");
      }
    } catch (error) {
      // Fail closed if current authorization or session status cannot be checked.
      if (room) await disconnect("Session status could not be checked. Video disconnected; you can continue in writing.");
      else message(error.message);
    } finally { polling = false; }
  }, 2000);
  window.addEventListener("pagehide", () => {
    window.clearInterval(poll);
    stopLocal();
    if (room) void room.disconnect();
  });
})();
