import { Children, useRef, useState, type ReactNode } from 'react';
import { Check, Copy } from 'lucide-react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { RichAiText, type AiTaskReference } from './RichAiText';

function CodeBlock({ children }: { children: ReactNode }) {
  const codeRef = useRef<HTMLPreElement>(null);
  const [copyState, setCopyState] = useState<'idle' | 'copied' | 'failed'>('idle');
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(codeRef.current?.textContent ?? '');
      setCopyState('copied');
    } catch { setCopyState('failed'); }
  };
  return <div className="chat-code-block">
    <div className="chat-code-toolbar"><span>Code</span><button type="button" onClick={() => void copy()} aria-label="Copy code">{copyState === 'copied' ? <Check size={14} /> : <Copy size={14} />}<span role="status">{copyState === 'copied' ? 'Copied' : copyState === 'failed' ? 'Try copying again' : 'Copy'}</span></button></div>
    <pre ref={codeRef}>{children}</pre>
  </div>;
}

export function ChatMarkdown({ text, taskReferences = [] }: { text: string; taskReferences?: AiTaskReference[] }) {
  const rich = (children: ReactNode) => Children.map(children, child => typeof child === 'string'
    ? <RichAiText text={child} taskReferences={taskReferences} /> : child);
  return <div className="chat-markdown">
    <Markdown remarkPlugins={[remarkGfm]} skipHtml components={{
      p: ({ children }) => <p>{rich(children)}</p>,
      li: ({ children }) => <li>{rich(children)}</li>,
      strong: ({ children }) => <strong>{rich(children)}</strong>,
      em: ({ children }) => <em>{rich(children)}</em>,
      a: ({ href, children }) => <a href={href} target="_blank" rel="noopener noreferrer">{children}</a>,
      img: ({ alt }) => <span>{alt}</span>,
      pre: ({ children }) => <CodeBlock>{children}</CodeBlock>,
      table: ({ children }) => <div className="chat-table-scroll" role="region" aria-label="Response table" tabIndex={0}><table>{children}</table></div>,
    }}>{text}</Markdown>
  </div>;
}
