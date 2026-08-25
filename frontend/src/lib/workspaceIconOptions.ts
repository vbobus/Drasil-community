import type { LucideIcon } from 'lucide-react';
import {
  BookOpen,
  BriefcaseBusiness,
  Circle,
  CircleDollarSign,
  Coffee,
  Dumbbell,
  Gamepad2,
  GraduationCap,
  HeartPulse,
  House,
  Lightbulb,
  Music2,
  Palette,
  PawPrint,
  Plane,
  Rocket,
  ShoppingBasket,
  Sparkles,
  Sprout,
  Star,
  Target,
  Trees,
  TrendingUp,
  Utensils,
  Users,
  Wrench,
} from 'lucide-react';

export interface WorkspaceIconOption {
  slug: string;
  label: string;
  Icon: LucideIcon;
}

export const workspaceIconOptions: WorkspaceIconOption[] = [
  { slug: 'circle', label: 'General', Icon: Circle },
  { slug: 'briefcase', label: 'Work', Icon: BriefcaseBusiness },
  { slug: 'heart', label: 'Health', Icon: HeartPulse },
  { slug: 'home', label: 'Home', Icon: House },
  { slug: 'dollar', label: 'Finance', Icon: CircleDollarSign },
  { slug: 'users', label: 'Social', Icon: Users },
  { slug: 'trending', label: 'Growth', Icon: TrendingUp },
  { slug: 'coffee', label: 'Leisure', Icon: Coffee },
  { slug: 'target', label: 'Goals', Icon: Target },
  { slug: 'book-open', label: 'Reading', Icon: BookOpen },
  { slug: 'graduation-cap', label: 'Learning', Icon: GraduationCap },
  { slug: 'dumbbell', label: 'Fitness', Icon: Dumbbell },
  { slug: 'utensils', label: 'Food', Icon: Utensils },
  { slug: 'shopping-basket', label: 'Shopping', Icon: ShoppingBasket },
  { slug: 'plane', label: 'Travel', Icon: Plane },
  { slug: 'music', label: 'Music', Icon: Music2 },
  { slug: 'palette', label: 'Creative', Icon: Palette },
  { slug: 'gamepad', label: 'Gaming', Icon: Gamepad2 },
  { slug: 'paw-print', label: 'Pets', Icon: PawPrint },
  { slug: 'sprout', label: 'Habits', Icon: Sprout },
  { slug: 'trees', label: 'Outdoors', Icon: Trees },
  { slug: 'lightbulb', label: 'Ideas', Icon: Lightbulb },
  { slug: 'wrench', label: 'Projects', Icon: Wrench },
  { slug: 'rocket', label: 'Launch', Icon: Rocket },
  { slug: 'sparkles', label: 'Personal', Icon: Sparkles },
  { slug: 'star', label: 'Favorites', Icon: Star },
];

const workspaceIconBySlug = new Map(
  workspaceIconOptions.map((option) => [option.slug, option.Icon]),
);

export function resolveWorkspaceIcon(slug: string): LucideIcon {
  return workspaceIconBySlug.get(slug) ?? Circle;
}
