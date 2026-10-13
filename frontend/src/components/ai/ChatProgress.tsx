import { useState } from 'react';
import { ChevronDown, LoaderCircle } from 'lucide-react';
import { ChatMarkdown } from './ChatMarkdown';

export function ChatProgress({ reasoning, activity, running }: { reasoning?: string; activity: string; running: boolean }) {
  const [expanded, setExpanded] = useState(running);
  return <div className="chat-progress">
    {running && <div className="chat-progress-status" role="status"><LoaderCircle size={15} className="chat-progress-spinner" aria-hidden="true" /><span>{activity}</span></div>}
    {reasoning && <details className="chat-reasoning" open={expanded} onToggle={event => setExpanded(event.currentTarget.open)}>
      <summary><ChevronDown size={14} aria-hidden="true" />Thinking summary</summary>
      <div className="chat-reasoning-text"><ChatMarkdown text={reasoning} /></div>
    </details>}
  </div>;
}
