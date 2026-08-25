import { searchBestMatchingEmojis } from 'emoogle-emoji-search-engine';
import type { NodeType } from '../types/node';

const FALLBACK_EMOJI_BY_TYPE: Record<NodeType, string> = {
  task: '📝',
  habit: '🔁',
};

const CUSTOM_EMOJI_KEYWORDS: Record<string, string[]> = {
  '🧠': ['plan', 'planning', 'strategy', 'strategic', 'think', 'review', 'refine'],
  '✍️': ['write', 'draft', 'outline', 'copy'],
  '🔎': ['research', 'analyze', 'analysis', 'background'],
  '🎨': ['design', 'visual', 'brand', 'creative', 'ui', 'ux'],
  '💼': ['client', 'portfolio', 'case study', 'job', 'work'],
  '🔥': ['priority', 'urgent', 'important'],
  '📅': ['calendar', 'schedule', 'timebox', 'plan day'],
  '🏃': ['exercise', 'run', 'movement', 'workout', 'fitness'],
};

const emojiSuggestionCache = new Map<string, string>();

export function suggestEmoji(title: string, type: NodeType = 'task') {
  const query = title.trim();
  if (!query) {
    return FALLBACK_EMOJI_BY_TYPE[type];
  }

  const cacheKey = `${type}:${query.toLocaleLowerCase()}`;
  const cachedEmoji = emojiSuggestionCache.get(cacheKey);
  if (cachedEmoji) {
    return cachedEmoji;
  }

  const [topMatch] = searchBestMatchingEmojis(query, 1, {
    customEmojiKeywords: CUSTOM_EMOJI_KEYWORDS,
  });

  const emoji = topMatch ?? FALLBACK_EMOJI_BY_TYPE[type];
  emojiSuggestionCache.set(cacheKey, emoji);
  return emoji;
}
