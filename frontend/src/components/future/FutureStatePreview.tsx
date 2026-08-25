import './FutureStatePreview.css';

const workspaceLanes = [
  {
    title: 'Focus stack',
    accentClass: 'is-work',
    items: [
      'Launch onboarding diagnostics',
      'Refine frictionless reschedule flow',
      'Write migration note for OAuth tenants',
    ],
  },
  {
    title: 'Protected rhythms',
    accentClass: 'is-health',
    items: [
      'Morning reset routine',
      'Core sleep block',
      'Sunday weekly review',
    ],
  },
];

const scheduledBlocks = [
  {
    time: '08:30',
    end: '09:15',
    title: 'Planning ritual',
    meta: 'Protected habit',
    className: 'is-protected',
  },
  {
    time: '09:30',
    end: '11:00',
    title: 'Launch onboarding diagnostics',
    meta: 'Dragged from Work backlog',
    className: 'is-work',
  },
  {
    time: '11:15',
    end: '12:00',
    title: 'Collision resolver suggestion',
    meta: 'AI proposed swap with admin block',
    className: 'is-ai',
  },
  {
    time: '14:00',
    end: '15:00',
    title: 'Refine frictionless reschedule flow',
    meta: 'Pinned to shared calendar',
    className: 'is-growth',
  },
];

const reviewCards = [
  {
    title: 'Rollover detected',
    body: 'Two unfinished tasks were moved into today with neutral rollover styling and one-click reschedule suggestions.',
  },
  {
    title: 'Collision resolved',
    body: 'The calendar caught an overlap with your protected movement block and offered two alternatives before saving.',
  },
  {
    title: 'AI sandbox ready',
    body: 'Task breakdown ideas stay staged until you accept them into the tree or directly onto the calendar.',
  },
];

const aiSuggestions = [
  'Split "Prepare investor memo" into three 35-minute tasks.',
  'Move "Movement reset" after the admin block to avoid fatigue clustering.',
  'Convert unfinished Friday tasks into a Saturday soft-landing queue.',
];

export function FutureStatePreview() {
  return (
    <div className="future-preview">
      <header className="future-preview-hero">
        <div className="future-preview-hero-copy">
          <p className="future-preview-kicker">Vision Preview</p>
          <h1>Drasil after the major execution features land</h1>
          <p>
            This is a static direction-setting mockup only. It does not replace the live
            workspace and only appears at <code>?preview=future-state</code>.
          </p>
        </div>
        <div className="future-preview-status">
          <span className="future-status-pill">Tree + calendar + AI sandbox</span>
          <span className="future-status-pill">Collision handling</span>
          <span className="future-status-pill">Compassionate rollover review</span>
        </div>
      </header>

      <main className="future-preview-grid">
        <section className="future-panel future-panel-sidebar" aria-label="Workspace overview">
          <div className="future-brand">
            <span className="future-brand-mark">D</span>
            <div>
              <p className="future-panel-label">Workspace system</p>
              <h2>Category compartments</h2>
            </div>
          </div>

          <div className="future-workspace-list">
            <button className="future-workspace-item is-active" type="button">Work</button>
            <button className="future-workspace-item" type="button">Health</button>
            <button className="future-workspace-item" type="button">Home</button>
            <button className="future-workspace-item" type="button">Growth</button>
            <button className="future-workspace-item" type="button">Review</button>
          </div>

          <div className="future-mini-review">
            <p className="future-panel-label">Weekly signal</p>
            <h3>Execution confidence is steady</h3>
            <p>17 planned blocks honored, 3 gently rescheduled, 0 punitive alerts.</p>
          </div>
        </section>

        <section className="future-panel future-panel-tree" aria-label="Tree planning view">
          <div className="future-panel-header">
            <div>
              <p className="future-panel-label">Plan in compartments</p>
              <h2>Strategy tree and backlog</h2>
            </div>
            <button className="future-secondary-button" type="button">+ Add task</button>
          </div>

          <div className="future-lanes">
            {workspaceLanes.map((lane) => (
              <article key={lane.title} className={`future-lane ${lane.accentClass}`}>
                <h3>{lane.title}</h3>
                <ul>
                  {lane.items.map((item) => (
                    <li key={item}>{item}</li>
                  ))}
                </ul>
              </article>
            ))}
          </div>

          <div className="future-backlog">
            <div className="future-backlog-header">
              <span>Backlog</span>
              <span>6 items</span>
            </div>
            <div className="future-backlog-items">
              <span className="future-chip">Draft partner update</span>
              <span className="future-chip">Research habit streak heuristics</span>
              <span className="future-chip">Refactor admin telemetry cards</span>
            </div>
          </div>
        </section>

        <section className="future-panel future-panel-calendar" aria-label="Calendar execution view">
          <div className="future-panel-header">
            <div>
              <p className="future-panel-label">Execute through a shared calendar</p>
              <h2>Constraint-aware day canvas</h2>
            </div>
            <button className="future-primary-button" type="button">Resolve collision</button>
          </div>

          <div className="future-calendar">
            <div className="future-calendar-header">
              <span>Time</span>
              <span>Today</span>
              <span>Tomorrow</span>
            </div>
            <div className="future-calendar-body">
              <div className="future-calendar-rail">
                <span>08:00</span>
                <span>10:00</span>
                <span>12:00</span>
                <span>14:00</span>
                <span>16:00</span>
              </div>
              <div className="future-calendar-column is-today">
                {scheduledBlocks.map((block) => (
                  <article key={block.title} className={`future-calendar-block ${block.className}`}>
                    <p>{block.time} - {block.end}</p>
                    <h3>{block.title}</h3>
                    <span>{block.meta}</span>
                  </article>
                ))}
              </div>
              <div className="future-calendar-column">
                <article className="future-calendar-draft">
                  <p>AI draft queue</p>
                  <h3>Three suggested placements waiting for approval</h3>
                  <span>Nothing touches the real schedule until accepted.</span>
                </article>
              </div>
            </div>
          </div>
        </section>

        <section className="future-panel future-panel-ai" aria-label="AI review panel">
          <div className="future-panel-header">
            <div>
              <p className="future-panel-label">AI sandbox</p>
              <h2>Review before anything commits</h2>
            </div>
            <button className="future-secondary-button" type="button">Accept 2 changes</button>
          </div>

          <div className="future-ai-list">
            {aiSuggestions.map((suggestion) => (
              <article key={suggestion} className="future-ai-card">
                <p>{suggestion}</p>
                <div className="future-ai-actions">
                  <button type="button">Accept</button>
                  <button type="button">Edit</button>
                  <button type="button">Dismiss</button>
                </div>
              </article>
            ))}
          </div>
        </section>

        <section className="future-panel future-panel-review" aria-label="Review and rollover view">
          <div className="future-panel-header">
            <div>
              <p className="future-panel-label">Review by category</p>
              <h2>Compassionate follow-through</h2>
            </div>
          </div>

          <div className="future-review-grid">
            {reviewCards.map((card) => (
              <article key={card.title} className="future-review-card">
                <h3>{card.title}</h3>
                <p>{card.body}</p>
              </article>
            ))}
          </div>
        </section>
      </main>
    </div>
  );
}
