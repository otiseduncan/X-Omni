import { useEffect, useState } from "react";
import {
  CalendarDays,
  CheckSquare,
  ChevronDown,
  CloudSun,
  Cpu,
  FileText,
  FolderOpen,
  Lock,
  ShieldAlert,
  Terminal,
  Wrench,
} from "lucide-react";

/**
 * Tool rail: what X can actually do, and at what permission tier.
 *
 * Read-only tools are runnable straight from here -- one click, no model
 * turn, no tokens burned. Approval-gated and blocked tools are listed but
 * not clickable, so the rail doubles as an honest statement of the
 * capability surface rather than a menu that lies about it.
 */

const ICONS = {
  get_weather: CloudSun,
  get_calendar: CalendarDays,
  list_tasks: CheckSquare,
  add_task: CheckSquare,
  read_file: FileText,
  list_directory: FolderOpen,
  system_status: Cpu,
  write_file: FileText,
  create_calendar_event: CalendarDays,
  run_powershell: Terminal,
  delete_file: Lock,
};

const LABELS = {
  get_weather: "Weather",
  get_calendar: "Calendar",
  list_tasks: "Tasks",
  add_task: "Add task",
  read_file: "Read file",
  list_directory: "List folder",
  system_status: "System",
  write_file: "Write file",
  create_calendar_event: "New event",
  run_powershell: "PowerShell",
  delete_file: "Delete file",
};

// Tools that need no arguments can be fired directly from the rail.
const ONE_CLICK = new Set(["get_weather", "get_calendar", "list_tasks", "system_status"]);

export default function ToolRail({ onRun, disabled }) {
  const [tools, setTools] = useState(null);
  const [running, setRunning] = useState(null);
  // Collapsed by default -- the rail should read as one quiet line until
  // you actually want the tool list.
  const [open, setOpen] = useState(false);

  useEffect(() => {
    fetch("/api/tools", { credentials: "include" })
      .then((r) => (r.ok ? r.json() : null))
      .then(setTools)
      .catch(() => setTools(null));
  }, []);

  if (!tools) return null;

  const oneClickCount = tools.tools.filter(
    (t) => t.tier === "read_only" && t.implemented && ONE_CLICK.has(t.name)
  ).length;

  const groups = [
    { tier: "read_only", label: "Available" },
    { tier: "confirm_required", label: "Needs approval" },
    { tier: "blocked", label: "Blocked" },
  ];

  async function run(name) {
    if (!ONE_CLICK.has(name) || disabled) return;
    setRunning(name);
    try {
      await onRun(name);
    } finally {
      setRunning(null);
    }
  }

  return (
    <div className={`rail-block tool-rail${open ? " is-open" : ""}`}>
      <button
        className="tool-rail-toggle"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <Wrench size={13} />
        <span>Tools</span>
        <em>{oneClickCount} quick</em>
        <ChevronDown size={14} className="tool-rail-chevron" />
      </button>

      <div className="tool-rail-body">
      {groups.map(({ tier, label }) => {
        const items = tools.tools.filter((t) => t.tier === tier);
        if (!items.length) return null;
        return (
          <div className="tool-group" key={tier}>
            <p className="rail-sub">{label}</p>
            <div className="tool-list">
              {items.map((t) => {
                const Icon = ICONS[t.name] || Wrench;
                const clickable = ONE_CLICK.has(t.name) && tier === "read_only";
                return (
                  <button
                    key={t.name}
                    className={[
                      "tool-chip-btn",
                      `tier-${tier}`,
                      clickable ? "clickable" : "",
                      running === t.name ? "running" : "",
                    ]
                      .filter(Boolean)
                      .join(" ")}
                    onClick={() => run(t.name)}
                    disabled={!clickable || disabled || running !== null}
                    title={
                      clickable
                        ? `Run ${LABELS[t.name] || t.name} now`
                        : tier === "blocked"
                          ? "Blocked by policy — cannot run"
                          : tier === "confirm_required"
                            ? "X can request this; you approve it in chat"
                            : "Ask X to use this"
                    }
                  >
                    <Icon size={13} />
                    <span>{LABELS[t.name] || t.name}</span>
                    {tier === "confirm_required" && <ShieldAlert size={11} />}
                    {tier === "blocked" && <Lock size={11} />}
                  </button>
                );
              })}
            </div>
          </div>
        );
      })}

        <p className="rail-note" style={{ marginTop: 8 }}>
          Roots: {tools.roots.join(" · ")}
        </p>
      </div>
    </div>
  );
}
