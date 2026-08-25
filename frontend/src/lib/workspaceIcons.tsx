import { createElement } from 'react';
import { resolveWorkspaceIcon } from './workspaceIconOptions';

export function WorkspaceIcon({ slug, size = 18 }: { slug: string; size?: number }) {
  return createElement(resolveWorkspaceIcon(slug), {
    size,
    strokeWidth: 1.8,
    'aria-hidden': true,
  });
}
