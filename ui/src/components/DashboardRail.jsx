import { useCallback, useEffect, useState } from "react";
import {
  CalendarDays,
  CheckSquare,
  ChevronDown,
  CloudSun,
  RefreshCw,
} from "lucide-react";
import RadarMap from "./RadarMap.jsx";

/**
 * Always-visible Today rail: month calendar, weather, agenda, tasks.
 *
 * The month grid renders from the browser's own clock, so a calendar is
 * on screen from the first paint whether or not Google is connected and
 * whether or not a model worker is up. Google events, when connected,
 * decorate it with dots and fill the agenda beneath.
 *
 * Collapses to a tap-to-open summary on phones; permanent on desktop.
 */

const DAY_LABELS = ["S", "M", "T", "W", "T", "F", "S"];
const REFRESH_MS = 10 * 60 * 1000;

function isoDay(date) {
  // Local calendar day, not UTC -- toISOString() would roll the date over
  // for anyone west of Greenwich in the evening.
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, "0");
  const d = String(date.getDate()).padStart(2, "0");
  return `${y}-${m}-${d}`;
}

function MonthGrid({ eventDays, today }) {
  const [cursor, setCursor] = useState(() => new Date());

  const year = cursor.getFullYear();
  const month = cursor.getMonth();
  const first = new Date(year, month, 1);
  const daysInMonth = new Date(year, month + 1, 0).getDate();
  const leading = first.getDay();

  const cells = [];
  for (let i = 0; i < leading; i += 1) cells.push(null);
  for (let d = 1; d <= daysInMonth; d += 1) cells.push(d);

  const label = cursor.toLocaleDateString([], { month: "long", year: "numeric" });

  return (
    <div className="mini-cal">
      <div className="mini-cal-head">
        <button
          className="mini-cal-nav"
          onClick={() => setCursor(new Date(year, month - 1, 1))}
          aria-label="Previous month"
        >
          ‹
        </button>
        <strong>{label}</strong>
        <button
          className="mini-cal-nav"
          onClick={() => setCursor(new Date(year, month + 1, 1))}
          aria-label="Next month"
        >
          ›
        </button>
      </div>

      <div className="mini-cal-grid">
        {DAY_LABELS.map((d, i) => (
          <span className="mini-cal-dow" key={`${d}-${i}`}>
            {d}
          </span>
        ))}
        {cells.map((day, i) => {
          if (day === null) return <span key={`pad-${i}`} />;
          const key = isoDay(new Date(year, month, day));
          return (
            <span
              key={key}
              className={[
                "mini-cal-day",
                key === today ? "is-today" : "",
                eventDays.has(key) ? "has-event" : "",
              ]
                .filter(Boolean)
                .join(" ")}
            >
              {day}
            </span>
          );
        })}
      </div>
    </div>
  );
}

function LocationPrompt({ onSaved }) {
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  async function save(e) {
    e.preventDefault();
    const text = value.trim();
    if (!text) return;
    setBusy(true);
    setError(null);
    try {
      const isZip = /^\d{5}$/.test(text);
      const resp = await fetch("/api/weather/location", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify(isZip ? { zip: text } : { name: text }),
      });
      const payload = await resp.json();
      if (!resp.ok) throw new Error(payload.detail || "Could not set location.");
      onSaved(payload.forecast);
    } catch (err) {
      setError(String(err.message || err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="wx-setup" onSubmit={save}>
      <input
        value={value}
        onChange={(e) => setValue(e.target.value)}
        placeholder="City or ZIP"
        aria-label="Weather location"
      />
      <button type="submit" disabled={busy || !value.trim()}>
        {busy ? "…" : "Set"}
      </button>
      {error && <p className="rail-note error">{error}</p>}
    </form>
  );
}

export default function DashboardRail({ children }) {
  const [weather, setWeather] = useState(null);
  const [calendar, setCalendar] = useState(null);
  const [tasks, setTasks] = useState(null);
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);

  const today = isoDay(new Date());

  const load = useCallback(async () => {
    setLoading(true);
    const get = async (url) => {
      try {
        const r = await fetch(url, { credentials: "include" });
        return r.ok ? await r.json() : null;
      } catch {
        return null;
      }
    };
    const [w, c] = await Promise.all([get("/api/weather"), get("/api/calendar?days=30")]);
    setWeather(w);
    setCalendar(c);
    setLoading(false);
  }, []);

  useEffect(() => {
    load();
    const timer = window.setInterval(load, REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [load]);

  const events = calendar?.events || [];
  const eventDays = new Set(events.map((e) => String(e.start || "").slice(0, 10)));
  const todayEvents = events.filter((e) => String(e.start || "").startsWith(today));
  const upcoming = events.filter((e) => !String(e.start || "").startsWith(today)).slice(0, 4);

  const cur = weather?.current;
  const summaryBits = [];
  if (cur?.temperature_f != null) summaryBits.push(`${Math.round(cur.temperature_f)}°`);
  summaryBits.push(
    todayEvents.length
      ? `${todayEvents.length} event${todayEvents.length === 1 ? "" : "s"} today`
      : "nothing today"
  );

  return (
    <section className={`rail${open ? " is-open" : ""}`}>
      <button
        className="rail-toggle"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <CalendarDays size={14} />
        <span>Today</span>
        <em>{summaryBits.join(" · ")}</em>
        <ChevronDown size={15} className="rail-chevron" />
      </button>

      <div className="rail-body">
        {/* ---- weather ---- */}
        <div className="rail-block">
          <div className="rail-head">
            <CloudSun size={13} />
            <span>Weather</span>
            <button
              className="rail-refresh"
              onClick={load}
              disabled={loading}
              aria-label="Refresh"
            >
              <RefreshCw size={12} className={loading ? "spin" : ""} />
            </button>
          </div>

          {weather?.ok ? (
            <>
              <div className="rail-wx">
                <strong>
                  {cur?.temperature_f != null ? `${Math.round(cur.temperature_f)}°` : "--"}
                </strong>
                <div>
                  <span className="rail-wx-cond">{cur?.condition || "—"}</span>
                  <span className="rail-note">{weather.location?.name}</span>
                </div>
              </div>
              <div className="rail-wx-days">
                {(weather.forecast || []).slice(0, 5).map((d) => (
                  <div key={d.date}>
                    <span>
                      {new Date(`${d.date}T12:00:00`).toLocaleDateString([], {
                        weekday: "narrow",
                      })}
                    </span>
                    <strong>{d.high_f != null ? Math.round(d.high_f) : "--"}</strong>
                  </div>
                ))}
              </div>

              <RadarMap
                lat={weather.location?.latitude}
                lon={weather.location?.longitude}
              />
            </>
          ) : weather?.status === "not_configured" ? (
            <LocationPrompt onSaved={setWeather} />
          ) : (
            <p className="rail-note">{weather?.summary || "Weather unavailable."}</p>
          )}
        </div>

        {/* ---- calendar ---- */}
        <div className="rail-block">
          <div className="rail-head">
            <CalendarDays size={13} />
            <span>Calendar</span>
          </div>

          <MonthGrid eventDays={eventDays} today={today} />

          {calendar?.ok === false && (
            <p className="rail-note">
              {calendar.message || "Google Calendar isn't connected."}
            </p>
          )}

          {calendar?.ok && (
            <div className="rail-agenda">
              <p className="rail-sub">Today</p>
              {todayEvents.length === 0 ? (
                <p className="rail-note">Nothing scheduled.</p>
              ) : (
                todayEvents.map((e) => (
                  <div className="rail-event" key={e.id}>
                    <span>
                      {e.all_day
                        ? "all day"
                        : new Date(e.start).toLocaleTimeString([], {
                            hour: "numeric",
                            minute: "2-digit",
                          })}
                    </span>
                    <strong>{e.title}</strong>
                  </div>
                ))
              )}

              {upcoming.length > 0 && (
                <>
                  <p className="rail-sub">Next</p>
                  {upcoming.map((e) => (
                    <div className="rail-event" key={e.id}>
                      <span>
                        {new Date(e.start).toLocaleDateString([], {
                          month: "short",
                          day: "numeric",
                        })}
                      </span>
                      <strong>{e.title}</strong>
                    </div>
                  ))}
                </>
              )}
            </div>
          )}
        </div>

        {/* tool rail slots in beneath calendar */}
        {children}
      </div>
    </section>
  );
}
