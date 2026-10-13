import { useEffect, useMemo, useRef, useState } from 'react';
import type { CSSProperties, KeyboardEvent } from 'react';
import { useCategoryStore } from '../../store/useCategoryStore';
import { useUiStore, type AppDestination } from '../../store/useUiStore';
import type { Category } from '../../types/category';
import drasilLogoUrl from '../../assets/drasil-logo-transparent.png';
import { WorkspaceIcon } from '../../lib/workspaceIcons';
import { workspaceIconOptions } from '../../lib/workspaceIconOptions';
import { Layers3, Sprout, X } from 'lucide-react';
import { LogOut } from 'lucide-react';
import { useDrasilAccount } from '../account/AccountContext';
import './AppSidebar.css';

interface UtilityDestination {
  id: Exclude<AppDestination, 'all' | 'category'>;
  label: string;
  icon: string;
  description: string;
}

const utilityDestinations: UtilityDestination[] = [
  {
    id: 'my-day',
    label: 'My Day',
    icon: '\u263c',
    description: 'A focused daily queue built from selected and scheduled work.',
  },
  {
    id: 'habits',
    label: 'Habits',
    icon: 'sprout',
    description: 'Daily check-ins, streaks, and rewards for showing up.',
  },
  {
    id: 'inbox',
    label: 'Inbox',
    icon: '\u2709',
    description: 'A landing place for untriaged work before it gets assigned to a category.',
  },
  {
    id: 'settings',
    label: 'Settings',
    icon: '\u2699',
    description: 'System preferences, account controls, and future integrations.',
  },
];

function useAutoFocusInput<T extends HTMLElement>(isActive: boolean) {
  const ref = useRef<T>(null);

  useEffect(() => {
    if (isActive) {
      ref.current?.focus();
    }
  }, [isActive]);

  return ref;
}

function SidebarCollapseArrow() {
  return (
    <svg viewBox="0 0 20 20" className="sidebar-edge-toggle-icon" aria-hidden="true">
      <path d="M12.5 5.5 8 10l4.5 4.5" />
    </svg>
  );
}

function DrasilWordmark() {
  return (
    <span className="sidebar-wordmark" aria-label="Drasil">
      Drasil
    </span>
  );
}

interface AppSidebarProps {
  isCollapsed: boolean;
  onToggleCollapse: () => void;
  isMobile?: boolean;
  onNavigate?: () => void;
}

export function AppSidebar({
  isCollapsed,
  onToggleCollapse,
  isMobile = false,
  onNavigate,
}: AppSidebarProps) {
  const {
    categories,
    activeCategory,
    setActiveCategory,
    addCategory,
  } = useCategoryStore();
  const { activeDestination, setActiveDestination } = useUiStore();
  const { account, openAccountSwitcher, signOut } = useDrasilAccount();

  const [isAdding, setIsAdding] = useState(false);
  const [newName, setNewName] = useState('');
  const [newIconSlug, setNewIconSlug] = useState('circle');
  const [iconSearch, setIconSearch] = useState('');

  const addInputRef = useAutoFocusInput<HTMLInputElement>(isAdding);
  const filteredIconOptions = useMemo(() => {
    const query = iconSearch.trim().toLocaleLowerCase();
    return query
      ? workspaceIconOptions.filter((option) =>
          `${option.label} ${option.slug}`.toLocaleLowerCase().includes(query),
        )
      : workspaceIconOptions;
  }, [iconSearch]);

  const handleCategorySelect = (category: Category) => {
    onNavigate?.();
    setActiveCategory(category);
    setActiveDestination('category');
  };

  const handleUtilitySelect = (destination: UtilityDestination['id']) => {
    onNavigate?.();
    setActiveDestination(destination);
  };

  const handleAddSubmit = async () => {
    const trimmed = newName.trim();
    if (trimmed) {
      await addCategory(trimmed, newIconSlug);
      setActiveDestination('category');
    }
    setNewName('');
    setNewIconSlug('circle');
    setIconSearch('');
    setIsAdding(false);
  };

  const handleAddKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'Enter') {
      void handleAddSubmit();
    }
    if (event.key === 'Escape') {
      setNewName('');
      setNewIconSlug('circle');
      setIconSearch('');
      setIsAdding(false);
    }
  };

  return (
    <aside
      className={`sidebar${isCollapsed ? ' collapsed' : ''}${
        isAdding ? ' is-adding-workspace' : ''
      }`}
      aria-label="Primary workspace navigation"
    >
      <div className="sidebar-header">
        <button
          type="button"
          className={`sidebar-logo-btn${isCollapsed ? ' collapsed' : ' expanded'}`}
          aria-label={isCollapsed ? 'Expand sidebar' : 'Drasil workspace'}
          aria-expanded={isCollapsed ? false : undefined}
          title={isCollapsed ? 'Expand sidebar' : 'Drasil'}
          onClick={isCollapsed ? onToggleCollapse : () => { onNavigate?.(); setActiveDestination('category'); }}
        >
          {isCollapsed ? (
            <img src={drasilLogoUrl} alt="" className="sidebar-logo-img" />
          ) : (
            <DrasilWordmark />
          )}
        </button>

        {!isCollapsed && (
          <button
            type="button"
            className="sidebar-header-toggle"
            onClick={onToggleCollapse}
            aria-label={isMobile ? 'Close navigation' : 'Collapse sidebar'}
            aria-expanded
            title={isMobile ? 'Close navigation' : 'Collapse sidebar'}
          >
            {isMobile ? <X size={22} aria-hidden="true" /> : <SidebarCollapseArrow />}
          </button>
        )}
      </div>

      <nav className="sidebar-nav" aria-label="Primary workspace destinations">
        <section className="sidebar-section" aria-labelledby="sidebar-workspaces-heading">
          {!isCollapsed && (
            <div className="sidebar-nav-label" id="sidebar-workspaces-heading">
              Workspaces
            </div>
          )}

          <div className="sidebar-item-stack">
            <div
              className={`sidebar-item-row${activeDestination === 'all' ? ' active' : ''}`}
              style={{ '--cat-color': '#6366f1' } as CSSProperties}
            >
              <button
                type="button"
                className="sidebar-item-main"
                onClick={() => { onNavigate?.(); setActiveDestination('all'); }}
                aria-current={activeDestination === 'all' ? 'page' : undefined}
                aria-label={`All workspace${activeDestination === 'all' ? ' (active)' : ''}`}
                title={isCollapsed ? 'All' : 'Tasks from every workspace'}
              >
                <span
                  className="sidebar-item-icon"
                  aria-hidden="true"
                  style={{ color: '#6366f1' }}
                >
                  <Layers3 size={18} strokeWidth={1.8} />
                </span>
                {!isCollapsed && (
                  <span className="sidebar-item-label sidebar-all-label">All</span>
                )}
              </button>
            </div>

            {categories.map((category) => {
              const isActive =
                activeDestination === 'category' && activeCategory?.id === category.id;

              return (
                <div
                  key={category.id}
                  className={`sidebar-item-row${isActive ? ' active' : ''}`}
                  style={{ '--cat-color': category.accentColor } as CSSProperties}
                >
                  <button
                    type="button"
                    className="sidebar-item-main"
                    onClick={() => handleCategorySelect(category)}
                    aria-current={isActive ? 'page' : undefined}
                    aria-label={`${category.name} workspace${isActive ? ' (active)' : ''}`}
                    title={category.name}
                  >
                    <span
                      className="sidebar-item-icon"
                      aria-hidden="true"
                      style={{ color: category.accentColor }}
                    >
                      <WorkspaceIcon slug={category.iconSlug} size={18} />
                    </span>
                    {!isCollapsed && <span className="sidebar-item-label">{category.name}</span>}
                  </button>
                </div>
              );
            })}
          </div>
        </section>

        <section
          className="sidebar-section sidebar-section-utilities"
          aria-labelledby="sidebar-utilities-heading"
          data-testid="sidebar-utilities"
        >
          {!isCollapsed && (
            <div className="sidebar-nav-label" id="sidebar-utilities-heading">
              Utilities
            </div>
          )}

          <div className="sidebar-item-stack">
            {utilityDestinations.map((destination) => {
              const isActive = activeDestination === destination.id;

              return (
                <button
                  key={destination.id}
                  type="button"
                  className={`sidebar-item-main sidebar-item-utility${isActive ? ' active' : ''}`}
                  onClick={() => handleUtilitySelect(destination.id)}
                  aria-current={isActive ? 'page' : undefined}
                  aria-label={`${destination.label}${isActive ? ' (active)' : ''}`}
                  title={isCollapsed ? destination.label : destination.description}
                >
                  <span className="sidebar-item-badge" aria-hidden="true">
                    {destination.id === 'habits' ? <Sprout size={18} strokeWidth={1.8} /> : destination.icon}
                  </span>
                  {!isCollapsed && <span className="sidebar-item-label">{destination.label}</span>}
                </button>
              );
            })}
            <a
              className="sidebar-item-main sidebar-item-utility"
              href="/agent-architecture"
              target="_blank"
              rel="noopener noreferrer"
              aria-label="Open agent architecture in a new tab"
              onClick={onNavigate}
              title={isCollapsed
                ? 'Agent architecture'
                : 'Temporary admin utility. Opens the live agent debugger in a new tab.'}
            >
              <span className="sidebar-item-badge" aria-hidden="true">{'\u2318'}</span>
              {!isCollapsed && <span className="sidebar-item-label">Agent architecture</span>}
            </a>
          </div>
        </section>
      </nav>

      {!isCollapsed && (
        <div className="sidebar-add-section">
          {isAdding ? (
            <div className="sidebar-add-editor">
              <input
                ref={addInputRef}
                className="sidebar-add-input"
                value={newName}
                placeholder="Workspace name..."
                onChange={(event) => setNewName(event.target.value)}
                onKeyDown={handleAddKeyDown}
                aria-label="New workspace name"
              />
              <label className="sidebar-icon-search">
                <span>Choose icon</span>
                <input
                  type="search"
                  value={iconSearch}
                  onChange={(event) => setIconSearch(event.target.value)}
                  placeholder="Search icons"
                  aria-label="Search workspace icons"
                />
              </label>
              <div className="sidebar-icon-grid" role="listbox" aria-label="Workspace icon">
                {filteredIconOptions.map((option) => (
                  <button
                    key={option.slug}
                    type="button"
                    role="option"
                    aria-selected={newIconSlug === option.slug}
                    className={newIconSlug === option.slug ? 'is-selected' : ''}
                    onClick={() => setNewIconSlug(option.slug)}
                    title={option.label}
                  >
                    <WorkspaceIcon slug={option.slug} size={17} />
                    <span>{option.label}</span>
                  </button>
                ))}
              </div>
              <div className="sidebar-add-actions">
                <button
                  type="button"
                  onClick={() => void handleAddSubmit()}
                  disabled={!newName.trim()}
                >
                  Create
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setNewName('');
                    setNewIconSlug('circle');
                    setIconSearch('');
                    setIsAdding(false);
                  }}
                >
                  Cancel
                </button>
              </div>
            </div>
          ) : (
            <button
              type="button"
              className="sidebar-add-btn"
              onClick={() => setIsAdding(true)}
              aria-label="Add workspace"
            >
              <span aria-hidden="true">+</span>
              <span>Add workspace</span>
            </button>
          )}
        </div>
      )}
      <div className="sidebar-account">
        <button
          type="button"
          className="sidebar-account-switch"
          onClick={() => { onNavigate?.(); openAccountSwitcher(); }}
          title={`Switch account (signed in as ${account.displayName})`}
          aria-label={`Switch account. Signed in as ${account.displayName}`}
        >
          <span className="sidebar-account-avatar" aria-hidden="true">
            {account.displayName.slice(0, 1).toLocaleUpperCase()}
          </span>
          {!isCollapsed && (
            <span className="sidebar-account-copy">
              <strong>{account.displayName}</strong>
              <small>Switch account</small>
            </span>
          )}
        </button>
        {!isCollapsed && (
          <button
            type="button"
            className="sidebar-account-logout"
            onClick={() => { onNavigate?.(); void signOut(); }}
            title="Sign out"
            aria-label="Sign out"
          >
            <LogOut size={15} aria-hidden="true" />
          </button>
        )}
      </div>
    </aside>
  );
}
