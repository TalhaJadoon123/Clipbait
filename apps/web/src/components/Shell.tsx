import { NavLink, Outlet, useLocation } from "react-router-dom";
import { useEffect, useState } from "react";
import { api } from "../lib/api";

const NAV = [
  { to: "/", label: "Projects", end: true, glyph: "▦" },
  { to: "/new", label: "New clip", glyph: "+" },
  { to: "/queue", label: "Money queue", glyph: "$" },
  { to: "/live", label: "Live", glyph: "◉" },
  { to: "/insights", label: "Playbooks", glyph: "✦" },
];

export function Shell() {
  const [online, setOnline] = useState<boolean | null>(null);
  const [open, setOpen] = useState(false);
  const location = useLocation();

  useEffect(() => {
    let alive = true;
    api
      .meta()
      .then(() => alive && setOnline(true))
      .catch(() => alive && setOnline(false));
    return () => {
      alive = false;
    };
  }, []);

  useEffect(() => {
    setOpen(false);
  }, [location.pathname]);

  return (
    <div className="shell">
      <aside className={`sidebar${open ? " is-open" : ""}`}>
        <NavLink to="/" className="brand" aria-label="Clipbait home">
          <span className="brand-mark">C</span>
          <span className="brand-copy">
            <strong>clipbait</strong>
            <small>bait the scroll</small>
          </span>
        </NavLink>

        <nav className="nav" aria-label="Main">
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              className={({ isActive }) => `nav-item${isActive ? " is-active" : ""}`}
            >
              <span className="nav-glyph">{item.glyph}</span>
              {item.label}
            </NavLink>
          ))}
        </nav>

        <div className="sidebar-foot">
          <div className="engine-state">
            <span className={`dot ${online ? "dot-live" : online === false ? "dot-dead" : ""}`} />
            {online === null ? "Connecting" : online ? "Local engine" : "API offline"}
          </div>
          <p className="sidebar-note">
            No account. No cloud. Your footage never leaves this machine unless you add an API key.
          </p>
        </div>
      </aside>

      <div className="main">
        <header className="topbar">
          <button className="menu-btn" onClick={() => setOpen((value) => !value)} aria-label="Menu">
            ☰
          </button>
          <span className="topbar-kicker">STUDIO</span>
          <div className="topbar-right">
            <a className="ghost-link" href="/docs" target="_blank" rel="noreferrer">
              API docs ↗
            </a>
          </div>
        </header>
        <main className="content">
          <Outlet />
        </main>
      </div>
    </div>
  );
}