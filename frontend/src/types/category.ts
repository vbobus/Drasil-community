// frontend/src/types/category.ts
// Mirrors the backend CategoryRead schema — camelCase to match JSON wire format.

export interface Category {
  id: number;
  name: string;
  accentColor: string; // e.g. "#6366f1"
  iconSlug: string;    // e.g. "briefcase"
  sortOrder: number;
}

export type CategoryCreate = Pick<Category, 'name' | 'iconSlug'>;
export type CategoryUpdate = Partial<Pick<Category, 'name' | 'accentColor' | 'iconSlug'>>;
