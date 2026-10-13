// frontend/src/api/categories.ts
// The ONLY file allowed to call fetch() for the /api/v1/categories endpoints.
// Components → Zustand store → this file. Never call fetch() from components directly.

import type { Category, CategoryUpdate } from '../types/category';
import { categoryAccent } from '../lib/categoryPalette';
import { apiRequest } from './http';

const BASE = '/api/v1/categories';

/** Fetch all categories sorted by sort_order. */
export async function fetchCategories(): Promise<Category[]> {
  return (await apiRequest<Category[]>(BASE)).map(category => ({ ...category, accentColor: categoryAccent(category.accentColor) }));
}

/** Create a new category workspace. */
export async function createCategory(name: string, iconSlug = 'circle'): Promise<Category> {
  const category = await apiRequest<Category>(BASE, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, iconSlug }),
  });
  return { ...category, accentColor: categoryAccent(category.accentColor) };
}

/** Update a category's name or accent color. */
export async function updateCategory(
  id: number,
  updates: CategoryUpdate
): Promise<Category> {
  const category = await apiRequest<Category>(`${BASE}/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(updates),
  });
  return { ...category, accentColor: categoryAccent(category.accentColor) };
}

/** Delete a category workspace. */
export async function deleteCategory(id: number): Promise<void> {
  return apiRequest<void>(`${BASE}/${id}`, { method: 'DELETE' });
}
