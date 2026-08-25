// frontend/src/store/useCategoryStore.ts
// Zustand store for category workspaces.
// RULES:
//  - All state changes via named action functions — NO direct mutation from components.
//  - Components call store actions only; store actions call the api/ wrappers.
//  - activeCategory drives what the main content area displays.

import { create } from 'zustand';
import type { Category } from '../types/category';
import * as categoriesApi from '../api/categories';

const CATEGORY_FETCH_RETRY_DELAYS_MS = [250, 750, 1500, 3000];

function wait(ms: number): Promise<void> {
  return new Promise((resolve) => {
    window.setTimeout(resolve, ms);
  });
}

interface CategoryState {
  categories: Category[];
  activeCategory: Category | null;
  isLoading: boolean;
  error: string | null;
}

interface CategoryActions {
  fetchCategories: () => Promise<void>;
  setActiveCategory: (category: Category) => void;
  addCategory: (name: string, iconSlug?: string) => Promise<void>;
  updateCategory: (id: number, name: string) => Promise<boolean>;
  deleteCategory: (id: number) => Promise<boolean>;
}

type CategoryStore = CategoryState & CategoryActions;

export const useCategoryStore = create<CategoryStore>((set, get) => ({
  // --- State ---
  categories: [],
  activeCategory: null,
  isLoading: false,
  error: null,

  // --- Actions ---

  fetchCategories: async () => {
    if (get().isLoading) {
      return;
    }
    set({ isLoading: true, error: null });
    let lastError: unknown = null;
    for (let attempt = 0; attempt <= CATEGORY_FETCH_RETRY_DELAYS_MS.length; attempt += 1) {
      try {
        const categories = await categoriesApi.fetchCategories();
        if (!Array.isArray(categories)) {
          throw new Error('Categories response was not a list.');
        }

        const current = get().activeCategory;
        const activeCategory =
          current && categories.find((c) => c.id === current.id)
            ? current
            : categories[0] ?? null;
        set({ categories, activeCategory, isLoading: false, error: null });
        return;
      } catch (error) {
        lastError = error;
        const retryDelay = CATEGORY_FETCH_RETRY_DELAYS_MS[attempt];
        if (retryDelay !== undefined) {
          await wait(retryDelay);
        }
      }
    }
    console.warn('Failed to load categories after retries.', lastError);
    set({
      error: 'Could not load workspaces. The backend may still be starting.',
      isLoading: false,
    });
  },

  setActiveCategory: (category) => {
    set({ activeCategory: category });
  },

  addCategory: async (name, iconSlug = 'circle') => {
    set({ isLoading: true, error: null });
    try {
      const created = await categoriesApi.createCategory(name, iconSlug);
      set((state) => ({
        categories: [...state.categories, created],
        activeCategory: state.activeCategory ?? created,
        isLoading: false,
      }));
    } catch {
      set({ error: 'Failed to create category.', isLoading: false });
    }
  },

  updateCategory: async (id, name) => {
    set({ error: null });
    try {
      const updated = await categoriesApi.updateCategory(id, { name });
      set((state) => ({
        categories: state.categories.map((c) => (c.id === id ? updated : c)),
        activeCategory:
          state.activeCategory?.id === id ? updated : state.activeCategory,
      }));
      return true;
    } catch {
      set({ error: 'Failed to update category.' });
      return false;
    }
  },

  deleteCategory: async (id) => {
    set({ error: null });
    try {
      await categoriesApi.deleteCategory(id);
      set((state) => {
        const filtered = state.categories.filter((c) => c.id !== id);
        const activeCategory =
          state.activeCategory?.id === id
            ? (filtered[0] ?? null)
            : state.activeCategory;
        return { categories: filtered, activeCategory };
      });
      return true;
    } catch {
      set({ error: 'Failed to delete category.' });
      return false;
    }
  },
}));
