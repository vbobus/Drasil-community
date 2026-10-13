// Stored legacy colours keep their identity; presentation follows the shared palette.
const CATEGORY_KEYS: Record<string, string> = {
  '#6366f1': 'work', '#10b981': 'health', '#f59e0b': 'home', '#0ea5e9': 'finance',
  '#ec4899': 'social', '#8b5cf6': 'growth', '#14b8a6': 'leisure',
};
export function categoryAccent(color: string) {
  const key = CATEGORY_KEYS[color.toLowerCase()];
  return key ? `var(--color-cat-${key})` : color;
}
export function categoryFill(color: string) {
  const accent = categoryAccent(color);
  return accent.startsWith('var(--color-cat-') ? accent.replace(')', '-fill)')
    : `color-mix(in srgb, ${color} 35%, var(--color-calendar-paper))`;
}
