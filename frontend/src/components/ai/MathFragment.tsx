import { useMemo } from 'react';
import katex from 'katex';
import 'katex/dist/katex.min.css';

interface MathFragmentProps {
  expression: string;
  displayMode: boolean;
  source: string;
}

/** Render one math token. This component is lazy-loaded only when math is present. */
export function MathFragment({ expression, displayMode, source }: MathFragmentProps) {
  const markup = useMemo(() => {
    try {
      return katex.renderToString(expression, {
        displayMode,
        throwOnError: false,
        strict: 'warn',
        trust: false,
        output: 'htmlAndMathml',
      });
    } catch {
      return null;
    }
  }, [displayMode, expression]);

  if (!markup) return <>{source}</>;

  const Component = displayMode ? 'div' : 'span';
  return (
    <Component
      className={displayMode ? 'ai-math ai-math-display' : 'ai-math ai-math-inline'}
      dangerouslySetInnerHTML={{ __html: markup }}
    />
  );
}
