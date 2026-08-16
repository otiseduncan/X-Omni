import { useCallback, useEffect, useRef, useState } from "react";
import { ChevronDown, Pause, Play } from "lucide-react";

/**
 * Live local weather radar, centred on the saved weather location.
 *
 * Radar frames come from RainViewer's public API (no key, no account),
 * layered over a dark CARTO basemap so it matches the theme. Tiles are
 * positioned by hand with slippy-map maths rather than pulling in Leaflet
 * — a handful of <img> tags does the job and keeps the bundle small.
 *
 * Note: the browser fetches tiles directly from those CDNs, so they see
 * roughly where you are. That's inherent to any online map; nothing else
 * about X Omni leaves the machine because of it.
 */

const TILE = 256;
const FRAME_MS = 520;
const RAINVIEWER_API = "https://api.rainviewer.com/public/weather-maps.json";

function lonToTileX(lon, z) {
  return ((lon + 180) / 360) * 2 ** z;
}

function latToTileY(lat, z) {
  const r = (lat * Math.PI) / 180;
  return ((1 - Math.log(Math.tan(r) + 1 / Math.cos(r)) / Math.PI) / 2) * 2 ** z;
}

export default function RadarMap({ lat, lon }) {
  const [frames, setFrames] = useState([]);
  const [host, setHost] = useState("");
  const [index, setIndex] = useState(0);
  const [playing, setPlaying] = useState(true);
  const [zoom, setZoom] = useState(7);
  const [error, setError] = useState(null);
  const [size, setSize] = useState({ w: 0, h: 150 });
  const boxRef = useRef(null);

  // measure the container so tiles can be centred on the location
  useEffect(() => {
    const el = boxRef.current;
    if (!el) return undefined;
    const measure = () =>
      setSize({ w: el.clientWidth, h: el.clientHeight });
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const loadFrames = useCallback(async () => {
    try {
      const resp = await fetch(RAINVIEWER_API, { cache: "no-store" });
      if (!resp.ok) throw new Error(`radar index HTTP ${resp.status}`);
      const data = await resp.json();
      const past = data?.radar?.past || [];
      const nowcast = data?.radar?.nowcast || [];
      const all = [...past, ...nowcast];
      if (!all.length) throw new Error("no radar frames available");
      setHost(data.host || "https://tilecache.rainviewer.com");
      setFrames(all);
      // start on the most recent observed frame, not the oldest
      setIndex(Math.max(0, past.length - 1));
      setError(null);
    } catch (err) {
      setError(String(err.message || err));
    }
  }, []);

  useEffect(() => {
    loadFrames();
    // radar updates roughly every 10 minutes
    const t = window.setInterval(loadFrames, 10 * 60 * 1000);
    return () => window.clearInterval(t);
  }, [loadFrames]);

  useEffect(() => {
    if (!playing || frames.length < 2) return undefined;
    const t = window.setInterval(
      () => setIndex((i) => (i + 1) % frames.length),
      FRAME_MS
    );
    return () => window.clearInterval(t);
  }, [playing, frames.length]);

  if (lat == null || lon == null) return null;

  if (error) {
    return (
      <div className="radar">
        <div className="radar-fallback">Radar unavailable — {error}</div>
        <p className="radar-privacy">
          Online map: RainViewer and CARTO receive tile requests for this
          approximate area when radar is available.
        </p>
      </div>
    );
  }

  const fx = lonToTileX(lon, zoom);
  const fy = latToTileY(lat, zoom);
  const cx = Math.floor(fx);
  const cy = Math.floor(fy);

  // enough tiles to cover the box plus a ring of overscan
  const spanX = Math.ceil(size.w / TILE / 2) + 1;
  const spanY = Math.ceil(size.h / TILE / 2) + 1;

  const tiles = [];
  for (let tx = cx - spanX; tx <= cx + spanX; tx += 1) {
    for (let ty = cy - spanY; ty <= cy + spanY; ty += 1) {
      const max = 2 ** zoom;
      if (ty < 0 || ty >= max) continue;
      const wrappedX = ((tx % max) + max) % max;
      tiles.push({
        key: `${tx}-${ty}`,
        x: wrappedX,
        y: ty,
        left: tx * TILE - fx * TILE + size.w / 2,
        top: ty * TILE - fy * TILE + size.h / 2,
      });
    }
  }

  const frame = frames[index];
  const stamp = frame
    ? new Date(frame.time * 1000).toLocaleTimeString([], {
        hour: "numeric",
        minute: "2-digit",
      })
    : "";
  const isForecast = frame?.path?.includes("nowcast");

  return (
    <div className="radar">
      <div className="radar-box" ref={boxRef}>
        {size.w > 0 &&
          tiles.map((t) => (
            <img
              key={`base-${t.key}`}
              className="radar-tile"
              src={`https://basemaps.cartocdn.com/dark_all/${zoom}/${t.x}/${t.y}.png`}
              style={{ left: t.left, top: t.top }}
              alt=""
              loading="lazy"
              draggable={false}
            />
          ))}

        {size.w > 0 &&
          frame &&
          tiles.map((t) => (
            <img
              key={`radar-${t.key}-${frame.time}`}
              className="radar-tile radar-layer"
              src={`${host}${frame.path}/${TILE}/${zoom}/${t.x}/${t.y}/4/1_1.png`}
              style={{ left: t.left, top: t.top }}
              alt=""
              draggable={false}
            />
          ))}

        <div className="radar-pin" aria-label="Your location" />

        <div className="radar-controls">
          <button
            onClick={() => setPlaying((p) => !p)}
            aria-label={playing ? "Pause radar" : "Play radar"}
          >
            {playing ? <Pause size={11} /> : <Play size={11} />}
          </button>
          <span className={isForecast ? "forecast" : ""}>
            {stamp}
            {isForecast ? " (fcst)" : ""}
          </span>
          <button
            onClick={() => setZoom((z) => Math.max(5, z - 1))}
            disabled={zoom <= 5}
            aria-label="Zoom out"
          >
            −
          </button>
          <button
            onClick={() => setZoom((z) => Math.min(10, z + 1))}
            disabled={zoom >= 10}
            aria-label="Zoom in"
          >
            +
          </button>
        </div>
      </div>

      {frames.length > 1 && (
        <input
          className="radar-scrub"
          type="range"
          min={0}
          max={frames.length - 1}
          value={index}
          onChange={(e) => {
            setPlaying(false);
            setIndex(Number(e.target.value));
          }}
          aria-label="Radar time"
        />
      )}
      <p className="radar-privacy">
        Online map: RainViewer and CARTO receive tile requests for this
        approximate area.
      </p>
    </div>
  );
}
