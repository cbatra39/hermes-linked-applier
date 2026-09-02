/**
 * App shell: sidebar navigation, a live health strip, and the routes.
 *
 * The shell polls `/api/health` every 20s and turns it into three status dots
 * (LLM router, LinkedIn session, Docker sandbox). That polling lives here, at
 * the top, so every page gets the same answer from one request rather than
 * seven pages each running their own health probe.
 *
 * The LinkedIn banner is the one piece of persistent chrome. Until a session
 * exists, every scrape-backed feature fails, and the failure surfaces deep
 * inside a run log where it is easy to misread as a bug. Saying so up front,
 * with a link to the page that fixes it, is worth the vertical space.
 */

import { NavLink, Navigate, Route, Routes } from 'react-router-dom';

import {
  HermesMark,
  IconContainers,
  IconJobs,
  IconLinkedIn,
  IconOverview,
  IconResume,
  IconRuns,
  IconSettings,
  IconWarning,
  StatusDot,
} from './components';
import { api } from './lib/api';
import { cx } from './lib/format';
import { useApi } from './lib/hooks';
import type { Health } from './lib/types';
import type { StatusTone } from './components';

import Containers from './pages/Containers';
import Jobs from './pages/Jobs';
import LinkedInPage from './pages/LinkedIn';
import Overview from './pages/Overview';
import ResumePage from './pages/Resume';
import Runs from './pages/Runs';
import Settings from './pages/Settings';

const HEALTH_POLL_MS = 20_000;

interface NavItem {
  to: string;
  label: string;
  Icon: (props: { size?: number; className?: string }) => JSX.Element;
  end?: boolean;
}

const NAV: NavItem[] = [
  { to: '/', label: 'Overview', Icon: IconOverview, end: true },
  { to: '/jobs', label: 'Jobs', Icon: IconJobs },
  { to: '/resume', label: 'Resume', Icon: IconResume },
  { to: '/linkedin', label: 'LinkedIn', Icon: IconLinkedIn },
  { to: '/containers', label: 'Containers', Icon: IconContainers },
  { to: '/runs', label: 'Runs', Icon: IconRuns },
  { to: '/settings', label: 'Settings', Icon: IconSettings },
];

function toneFor(ok: boolean | undefined, degraded = false): StatusTone {
  if (ok) return degraded ? 'warn' : 'good';
  return ok === undefined ? 'idle' : 'bad';
}

export default function App() {
  const health = useApi<Health>((signal) => api.health(signal), [], {
    intervalMs: HEALTH_POLL_MS,
  });

  const data = health.data;
  const llm = data?.llm;
  const mcp = data?.mcp;

  // hermes-core emits both spellings for the LLM block; prefer `reachable`.
  const llmOk = llm ? Boolean(llm.reachable ?? llm.ok) : undefined;
  const mcpReachable = mcp ? Boolean(mcp.reachable) : undefined;
  const mcpAuthed = Boolean(mcp?.authenticated);
  const dockerOk = data ? Boolean(data.docker) : undefined;

  // Only nag once health has actually loaded — a banner that flashes on every
  // page load before the first response is noise, not information.
  const showLinkedInBanner = Boolean(data) && !mcpAuthed;

  return (
    <div className="flex min-h-screen bg-ink-900 text-ink-100">
      <aside className="fixed inset-y-0 left-0 z-30 hidden w-[var(--hermes-sidebar-w)] shrink-0 flex-col border-r border-ink-700 bg-ink-850 md:flex">
        <div className="flex items-center gap-2.5 border-b border-ink-700 px-4 py-4">
          <HermesMark size={24} className="text-brand-400" />
          <div className="min-w-0">
            <p className="truncate text-sm font-semibold tracking-tight text-ink-100">Hermes</p>
            <p className="truncate text-2xs text-ink-400">
              {data?.version ? `v${data.version}` : 'LinkedIn job agent'}
            </p>
          </div>
        </div>

        <nav className="flex-1 overflow-y-auto p-2" aria-label="Main">
          <ul className="flex flex-col gap-0.5">
            {NAV.map(({ to, label, Icon, end }) => (
              <li key={to}>
                <NavLink
                  to={to}
                  end={end}
                  className={({ isActive }) =>
                    cx(
                      'flex items-center gap-2.5 rounded-md px-2.5 py-2 text-sm transition-colors',
                      'focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-400',
                      isActive
                        ? 'bg-brand-600/15 font-medium text-brand-200'
                        : 'text-ink-300 hover:bg-ink-800 hover:text-ink-100',
                    )
                  }
                >
                  <Icon size={16} />
                  <span className="truncate">{label}</span>
                  {to === '/linkedin' && showLinkedInBanner && (
                    <span
                      className="ml-auto h-1.5 w-1.5 shrink-0 rounded-full bg-warn-500"
                      aria-label="LinkedIn not connected"
                    />
                  )}
                </NavLink>
              </li>
            ))}
          </ul>
        </nav>

        <div className="border-t border-ink-700 p-3">
          <p className="mb-2 text-2xs font-medium uppercase tracking-wider text-ink-500">
            Services
          </p>
          <ul className="flex flex-col gap-1.5 text-2xs">
            <li className="flex items-center gap-2">
              <StatusDot tone={toneFor(llmOk)} />
              <span className="text-ink-300">LLM router</span>
              <span className="ml-auto truncate text-ink-500">
                {llmOk ? (llm?.primary ?? 'ready') : llm?.key_configured ? 'down' : 'no key'}
              </span>
            </li>
            <li className="flex items-center gap-2">
              <StatusDot tone={toneFor(mcpReachable, mcpReachable && !mcpAuthed)} />
              <span className="text-ink-300">LinkedIn</span>
              <span className="ml-auto truncate text-ink-500">
                {!mcpReachable ? 'down' : mcpAuthed ? 'connected' : 'logged out'}
              </span>
            </li>
            <li className="flex items-center gap-2">
              <StatusDot tone={toneFor(dockerOk)} />
              <span className="text-ink-300">Sandbox</span>
              <span className="ml-auto truncate text-ink-500">{dockerOk ? 'ready' : 'no socket'}</span>
            </li>
          </ul>
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col md:pl-[var(--hermes-sidebar-w)]">
        {/* Compact nav for narrow screens; the sidebar is hidden below md. */}
        <div className="sticky top-0 z-20 border-b border-ink-700 bg-ink-850/95 backdrop-blur md:hidden">
          <div className="flex items-center gap-2 px-3 py-2.5">
            <HermesMark size={20} className="text-brand-400" />
            <span className="text-sm font-semibold">Hermes</span>
            <div className="ml-auto flex items-center gap-2">
              <StatusDot tone={toneFor(llmOk)} title="LLM router" />
              <StatusDot
                tone={toneFor(mcpReachable, mcpReachable && !mcpAuthed)}
                title="LinkedIn session"
              />
              <StatusDot tone={toneFor(dockerOk)} title="Docker sandbox" />
            </div>
          </div>
          <nav aria-label="Main" className="flex gap-1 overflow-x-auto px-2 pb-2">
            {NAV.map(({ to, label, end }) => (
              <NavLink
                key={to}
                to={to}
                end={end}
                className={({ isActive }) =>
                  cx(
                    'shrink-0 rounded px-2.5 py-1 text-xs transition-colors',
                    isActive ? 'bg-brand-600/20 text-brand-200' : 'text-ink-300 hover:text-ink-100',
                  )
                }
              >
                {label}
              </NavLink>
            ))}
          </nav>
        </div>

        {showLinkedInBanner && (
          <div className="flex items-start gap-2.5 border-b border-warn-600/30 bg-warn-600/10 px-4 py-2.5 text-xs text-warn-400">
            <IconWarning size={15} className="mt-px shrink-0" />
            <p className="min-w-0">
              <span className="font-medium">LinkedIn is not connected.</span>{' '}
              {mcpReachable
                ? 'Profile import and job search will fail until you sign in once through the login viewer.'
                : 'The linkedin-mcp container is unreachable — check that the stack is running.'}{' '}
              <NavLink to="/linkedin" className="underline underline-offset-2 hover:text-warn-400">
                Connect LinkedIn
              </NavLink>
            </p>
          </div>
        )}

        {health.error && !data && (
          <div className="border-b border-bad-600/30 bg-bad-600/10 px-4 py-2.5 text-xs text-bad-400">
            Cannot reach the Hermes API: {health.error}. Check{' '}
            <code className="font-mono">docker compose logs hermes-core</code>.
          </div>
        )}

        <main className="min-w-0 flex-1 px-4 py-5 md:px-6 md:py-6">
          <Routes>
            <Route path="/" element={<Overview />} />
            <Route path="/jobs" element={<Jobs />} />
            <Route path="/resume" element={<ResumePage />} />
            <Route path="/linkedin" element={<LinkedInPage />} />
            <Route path="/containers" element={<Containers />} />
            <Route path="/runs" element={<Runs />} />
            <Route path="/settings" element={<Settings />} />
            {/* Unknown paths go home rather than showing a dead screen. */}
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </main>
      </div>
    </div>
  );
}
