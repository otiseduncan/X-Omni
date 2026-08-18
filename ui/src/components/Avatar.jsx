import { useEffect, useRef, useState } from "react";

/**
 * Persistent avatar presence.
 *
 * States: idle | listening | thinking | speaking | swapping
 *
 * The swapping state is the one that earns its keep. A model swap takes
 * 15-20 seconds on this hardware; without a visible, distinct transition
 * that pause is indistinguishable from a hang.
 *
 * Falls back to a CSS orb if the video files aren't present, so the app
 * is fully usable before the avatar assets are dropped in.
 */

const CAPTIONS = {
  idle: "",
  listening: "listening",
  thinking: "thinking",
  speaking: "speaking",
  swapping: "switching model",
};

export default function Avatar({ state = "idle", worker, swapTarget, externalWorkload, swapSeconds }) {
  const [videoFailed, setVideoFailed] = useState(false);
  const videoRef = useRef(null);

  // Speaking gets a livelier loop when that asset exists; everything else
  // keeps the camera still and expresses state through the frame.
  const src = state === "speaking" ? "/avatar/speaking.mp4" : "/avatar/idle.mp4";

  useEffect(() => {
    const el = videoRef.current;
    if (!el) return;
    // Autoplay can be refused after a route change on mobile; retry quietly.
    const play = el.play();
    if (play?.catch) play.catch(() => {});
  }, [src]);

  let caption = CAPTIONS[state] || "";
  if (state === "swapping" && externalWorkload === "image_generation") caption = "generating image";
  else if (state === "swapping" && externalWorkload === "video_generation") caption = "rendering video";
  else if (state === "swapping" && swapTarget) caption = `switching to ${swapTarget}`;
  else if (state === "idle" && worker) caption = worker;
  if (state === "idle" && swapSeconds) caption = `${worker} · swapped in ${swapSeconds}s`;

  return (
    <div className="avatar-wrap">
      <div className="avatar" data-state={state} aria-label={`X Omni avatar, ${state}`}>
        {videoFailed ? (
          <div className="avatar-fallback">
            <div className="avatar-orb" />
          </div>
        ) : (
          <video
            ref={videoRef}
            key={src}
            src={src}
            autoPlay
            loop
            muted
            playsInline
            preload="auto"
            onError={() => setVideoFailed(true)}
          />
        )}
        <div className="avatar-glow" />
      </div>
      <div className={`avatar-caption${state === "swapping" ? " warn" : ""}`}>
        {caption}
      </div>
    </div>
  );
}
