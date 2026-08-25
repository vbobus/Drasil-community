import { faker } from '@faker-js/faker';
import type { Category } from '../../../../src/types/category';

const ACCENT_COLORS = ['#1f6feb', '#d97706', '#0f766e', '#9333ea', '#be123c'];
const ICON_SLUGS = ['briefcase', 'trending', 'users', 'coffee', 'circle'];

export class CategoryFactory {
  private nextId: number;
  private readonly createdIds = new Set<number>();

  constructor(seed = 20260509) {
    faker.seed(seed);
    this.nextId = 100;
  }

  build(overrides: Partial<Category> = {}): Category {
    const id = overrides.id ?? this.nextId++;

    this.createdIds.add(id);

    return {
      id,
      name: overrides.name ?? `${faker.word.adjective()} ${faker.word.noun()}`,
      accentColor: overrides.accentColor ?? faker.helpers.arrayElement(ACCENT_COLORS),
      iconSlug: overrides.iconSlug ?? faker.helpers.arrayElement(ICON_SLUGS),
      sortOrder: overrides.sortOrder ?? this.createdIds.size - 1,
    };
  }

  cleanup() {
    this.createdIds.clear();
    this.nextId = 100;
  }
}
