import { lazy, Suspense, useMemo } from 'react';
import type { CSSProperties, ReactNode } from 'react';
import { suggestEmoji } from '../../lib/emoji';
import './RichAiText.css';

const MathFragment = lazy(() =>
  import('./MathFragment').then((module) => ({ default: module.MathFragment })),
);

export interface AiTaskReference {
  node: {
    id: string;
    title: string;
    emoji?: string | null;
    type: 'task' | 'habit';
  };
  category: {
    name: string;
    accentColor: string;
  } | null;
}

interface RichAiTextProps {
  text: string;
  taskReferences?: AiTaskReference[];
}

interface MathToken {
  kind: 'math';
  source: string;
  expression: string;
  displayMode: boolean;
}

interface TextToken {
  kind: 'text';
  source: string;
}

type RichToken = MathToken | TextToken;

const MATH_TOKEN_RE = /(\$\$[\s\S]+?\$\$|\\\[[\s\S]+?\\\]|\$[^$\n]+?\$|\\\([^\n]+?\\\))/g;
const WORD_CHARACTER_RE = /[\p{L}\p{N}_]/u;
const GENERIC_TASK_NOUN_RE = /^\s+(?:task|tasks|item|items|option|options)\b/iu;
const EXPLICIT_PARENT_CONTEXT_RE = /(?:parent|root|project)\s+$/iu;

function tokenizeMath(text: string): RichToken[] {
  const tokens: RichToken[] = [];
  let cursor = 0;

  for (const match of text.matchAll(MATH_TOKEN_RE)) {
    const index = match.index ?? 0;
    if (index > cursor) {
      tokens.push({ kind: 'text', source: text.slice(cursor, index) });
    }

    const source = match[0];
    const displayMode = source.startsWith('$$') || source.startsWith('\\[');
    const expression = source.startsWith('$$')
      ? source.slice(2, -2)
      : source.startsWith('\\[') || source.startsWith('\\(')
        ? source.slice(2, -2)
        : source.slice(1, -1);
    tokens.push({ kind: 'math', source, expression, displayMode });
    cursor = index + source.length;
  }

  if (cursor < text.length) {
    tokens.push({ kind: 'text', source: text.slice(cursor) });
  }
  return tokens.length > 0 ? tokens : [{ kind: 'text', source: text }];
}

function isTitleBoundary(value: string | undefined) {
  return value === undefined || !WORD_CHARACTER_RE.test(value);
}

function isSharedTaskStem(
  reference: AiTaskReference,
  taskReferences: AiTaskReference[],
) {
  const title = reference.node.title.trim().toLocaleLowerCase();
  return taskReferences.some(({ node }) => {
    const candidate = node.title.trim().toLocaleLowerCase();
    return candidate.length > title.length
      && candidate.startsWith(title)
      && isTitleBoundary(candidate[title.length]);
  });
}

function isGenericSharedStemMention(
  text: string,
  index: number,
  titleLength: number,
  reference: AiTaskReference,
  taskReferences: AiTaskReference[],
) {
  if (!isSharedTaskStem(reference, taskReferences)) return false;
  const followingText = text.slice(index + titleLength, index + titleLength + 24);
  if (!GENERIC_TASK_NOUN_RE.test(followingText)) return false;
  const precedingText = text.slice(Math.max(0, index - 24), index);
  return !EXPLICIT_PARENT_CONTEXT_RE.test(precedingText);
}

function renderTaskReferences(
  text: string,
  taskReferences: AiTaskReference[],
  keyPrefix: string,
): ReactNode[] {
  const uniqueReferences = Array.from(
    new Map(
      taskReferences
        .filter(({ node }) => node.title.trim().length > 1)
        .map((reference) => [reference.node.title.trim().toLocaleLowerCase(), reference]),
    ).values(),
  ).sort((left, right) => right.node.title.length - left.node.title.length);

  if (uniqueReferences.length === 0) return [text];

  const normalizedText = text.toLocaleLowerCase();
  const rendered: ReactNode[] = [];
  let cursor = 0;
  let part = 0;

  while (cursor < text.length) {
    let nextMatch: { index: number; reference: AiTaskReference } | null = null;

    for (const reference of uniqueReferences) {
      const title = reference.node.title.trim().toLocaleLowerCase();
      let index = normalizedText.indexOf(title, cursor);
      while (index >= 0) {
        const before = index > 0 ? text[index - 1] : undefined;
        const afterIndex = index + title.length;
        const after = afterIndex < text.length ? text[afterIndex] : undefined;
        if (
          isTitleBoundary(before)
          && isTitleBoundary(after)
          && !isGenericSharedStemMention(
            text,
            index,
            title.length,
            reference,
            uniqueReferences,
          )
        ) break;
        index = normalizedText.indexOf(title, index + 1);
      }
      if (index >= 0 && (!nextMatch || index < nextMatch.index)) {
        nextMatch = { index, reference };
      }
    }

    if (!nextMatch) {
      rendered.push(text.slice(cursor));
      break;
    }
    const titleLength = nextMatch.reference.node.title.length;
    const hasEmphasisMarkers = (
      nextMatch.index >= 2
      && text.slice(nextMatch.index - 2, nextMatch.index) === '**'
      && text.slice(
        nextMatch.index + titleLength,
        nextMatch.index + titleLength + 2,
      ) === '**'
    );
    const prefixEnd = hasEmphasisMarkers
      ? nextMatch.index - 2
      : nextMatch.index;
    if (prefixEnd > cursor) {
      rendered.push(text.slice(cursor, prefixEnd));
    }

    const { node, category } = nextMatch.reference;
    rendered.push(
      <span
        key={`${keyPrefix}-task-${node.id}-${part}`}
        className="ai-task-reference"
        style={
          {
            '--task-reference-accent': category?.accentColor ?? '#6366f1',
          } as CSSProperties
        }
        title={category ? `${node.title} · ${category.name}` : node.title}
        data-task-id={node.id}
      >
        <span className="ai-task-reference-icon" aria-hidden="true">
          {node.emoji || suggestEmoji(node.title, node.type)}
        </span>
        <span>{node.title}</span>
      </span>,
    );
    cursor = nextMatch.index + node.title.length + (hasEmphasisMarkers ? 2 : 0);
    part += 1;
  }

  return rendered;
}

export function RichAiText({ text, taskReferences = [] }: RichAiTextProps) {
  const tokens = useMemo(() => tokenizeMath(text), [text]);

  return (
    <span className="rich-ai-text">
      {tokens.map((token, index) =>
        token.kind === 'math' ? (
          <Suspense key={`math-${index}-${token.source}`} fallback={token.source}>
            <MathFragment
              expression={token.expression}
              displayMode={token.displayMode}
              source={token.source}
            />
          </Suspense>
        ) : (
          <span key={`text-${index}`}>
            {renderTaskReferences(token.source, taskReferences, `text-${index}`)}
          </span>
        ),
      )}
    </span>
  );
}
