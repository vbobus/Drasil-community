import { useEffect, useMemo, useState } from 'react';
import {
  AlertTriangle,
  Pencil,
  Plus,
  Power,
  RotateCcw,
  Save,
  Search,
  Trash2,
  X,
} from 'lucide-react';
import {
  createRememberedPreference,
  deleteRememberedPreference,
  fetchRememberedPreferences,
  updateRememberedPreference,
  type RememberedPreference,
  type RememberedPreferencesResponse,
  type RememberedPreferenceWrite,
} from '../../api/preferences';

type PreferenceKind = 'text' | 'gap' | 'unavailable_window';

interface PreferenceFormState {
  description: string;
  domain: string;
  kind: PreferenceKind;
  preferenceType: string;
  scopeType: 'global' | 'task' | 'category';
  scopeId: string;
  strength: 'low' | 'medium' | 'strong' | 'high' | 'hard';
  gapMinutes: string;
  minimumTaskDurationMinutes: string;
  startTime: string;
  endTime: string;
  effectiveFrom: string;
  effectiveUntil: string;
  baseValue: Record<string, unknown>;
}

const EMPTY_FORM: PreferenceFormState = {
  description: '',
  domain: 'scheduling',
  kind: 'text',
  preferenceType: 'general',
  scopeType: 'global',
  scopeId: '',
  strength: 'medium',
  gapMinutes: '',
  minimumTaskDurationMinutes: '',
  startTime: '',
  endTime: '',
  effectiveFrom: '',
  effectiveUntil: '',
  baseValue: {},
};

function formatDateTime(value: string) {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return 'Unknown';
  return new Intl.DateTimeFormat('en-US', {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  }).format(parsed);
}

function labelFromToken(value: string) {
  return value.replace(/_/g, ' ').replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function getStringValue(value: Record<string, unknown>, key: string) {
  const raw = value[key];
  return typeof raw === 'string' && raw.trim() ? raw.trim() : null;
}

function getMinuteValue(value: Record<string, unknown>, key: string) {
  const raw = value[key];
  return typeof raw === 'number' && Number.isFinite(raw) && raw >= 0 ? raw : null;
}

function preferenceDetail(preference: RememberedPreference) {
  const constraint = preference.constraints[0];
  const value = constraint?.value ?? preference.value;
  const startTime = getStringValue(value, 'startTime');
  const endTime = getStringValue(value, 'endTime');
  if (startTime && endTime) return `${startTime} - ${endTime}`;
  const gapMinutes =
    getMinuteValue(value, 'interTaskGapMinutes') ??
    getMinuteValue(value, 'gapMinutes') ??
    getMinuteValue(value, 'bufferMinutes');
  const durationThreshold = getMinuteValue(value, 'minimumTaskDurationMinutes');
  if (gapMinutes !== null && durationThreshold !== null) {
    return `${gapMinutes}-minute gap for tasks lasting at least ${durationThreshold} minutes`;
  }
  if (gapMinutes !== null) return `${gapMinutes}-minute gap between tasks`;
  const preferenceText = getStringValue(preference.value, 'preferenceText');
  if (preferenceText) return preferenceText;
  const rule = getStringValue(preference.value, 'rule') ?? getStringValue(value, 'rule');
  return rule ? labelFromToken(rule) : labelFromToken(preference.preferenceType);
}

function preferenceTitle(preference: RememberedPreference) {
  return preference.description?.trim() || labelFromToken(preference.preferenceType);
}

function localInputValue(value?: string | null) {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  const offset = date.getTimezoneOffset() * 60_000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 16);
}

function formFromPreference(preference: RememberedPreference): PreferenceFormState {
  const value = preference.value;
  const gapMinutes =
    getMinuteValue(value, 'interTaskGapMinutes') ??
    getMinuteValue(value, 'gapMinutes') ??
    getMinuteValue(value, 'bufferMinutes');
  const startTime = getStringValue(value, 'startTime');
  const endTime = getStringValue(value, 'endTime');
  const kind: PreferenceKind = gapMinutes !== null
    ? 'gap'
    : startTime && endTime
      ? 'unavailable_window'
      : 'text';
  return {
    description: preferenceTitle(preference),
    domain: preference.domain,
    kind,
    preferenceType: preference.preferenceType,
    scopeType: preference.scopeType as PreferenceFormState['scopeType'],
    scopeId: preference.scopeId ?? '',
    strength: preference.strength as PreferenceFormState['strength'],
    gapMinutes: gapMinutes === null ? '' : String(gapMinutes),
    minimumTaskDurationMinutes: String(
      getMinuteValue(value, 'minimumTaskDurationMinutes') ?? '',
    ),
    startTime: startTime ?? '',
    endTime: endTime ?? '',
    effectiveFrom: localInputValue(preference.effectiveFrom),
    effectiveUntil: localInputValue(preference.effectiveUntil),
    baseValue: value,
  };
}

function formPayload(form: PreferenceFormState): RememberedPreferenceWrite {
  let value: Record<string, unknown>;
  let preferenceType = form.preferenceType || 'general';
  if (form.kind === 'gap') {
    preferenceType = 'inter_task_gap';
    value = { interTaskGapMinutes: Number(form.gapMinutes) };
    if (form.minimumTaskDurationMinutes) {
      value.minimumTaskDurationMinutes = Number(form.minimumTaskDurationMinutes);
      value.shorterTaskGapMinutes = 0;
    }
  } else if (form.kind === 'unavailable_window') {
    preferenceType = 'unavailable_window';
    value = { startTime: form.startTime, endTime: form.endTime };
  } else {
    value = { ...form.baseValue, preferenceText: form.description };
  }
  return {
    domain: form.domain.trim(),
    preferenceType,
    scopeType: form.scopeType,
    scopeId: form.scopeType === 'global' ? null : form.scopeId.trim(),
    strength: form.strength,
    description: form.description.trim(),
    value,
    effectiveFrom: form.effectiveFrom ? new Date(form.effectiveFrom).toISOString() : null,
    effectiveUntil: form.effectiveUntil ? new Date(form.effectiveUntil).toISOString() : null,
  };
}

function PreferenceEditor({
  form,
  onChange,
  onCancel,
  onSubmit,
  isSaving,
  isEditing,
}: {
  form: PreferenceFormState;
  onChange: (next: PreferenceFormState) => void;
  onCancel: () => void;
  onSubmit: () => void;
  isSaving: boolean;
  isEditing: boolean;
}) {
  const set = <K extends keyof PreferenceFormState>(key: K, value: PreferenceFormState[K]) => {
    onChange({ ...form, [key]: value });
  };
  const invalid = !form.description.trim()
    || !form.domain.trim()
    || (form.scopeType !== 'global' && !form.scopeId.trim())
    || (form.kind === 'gap' && (!form.gapMinutes || Number(form.gapMinutes) < 0))
    || (form.kind === 'unavailable_window' && (!form.startTime || !form.endTime));

  return (
    <form
      className="settings-memory-editor"
      onSubmit={(event) => {
        event.preventDefault();
        onSubmit();
      }}
    >
      <div className="settings-memory-editor-grid">
        <label className="settings-memory-field settings-memory-field-wide">
          <span>Preference</span>
          <textarea
            value={form.description}
            maxLength={512}
            rows={2}
            onChange={(event) => set('description', event.target.value)}
          />
        </label>
        <label className="settings-memory-field">
          <span>Rule type</span>
          <select value={form.kind} onChange={(event) => set('kind', event.target.value as PreferenceKind)}>
            <option value="text">Plain language</option>
            <option value="gap">Gap between tasks</option>
            <option value="unavailable_window">Unavailable window</option>
          </select>
        </label>
        <label className="settings-memory-field">
          <span>Domain</span>
          <input value={form.domain} maxLength={64} onChange={(event) => set('domain', event.target.value)} />
        </label>
        <label className="settings-memory-field">
          <span>Scope</span>
          <select
            value={form.scopeType}
            onChange={(event) => set('scopeType', event.target.value as PreferenceFormState['scopeType'])}
          >
            <option value="global">Global</option>
            <option value="category">Category</option>
            <option value="task">Task</option>
          </select>
        </label>
        {form.scopeType !== 'global' && (
          <label className="settings-memory-field">
            <span>{form.scopeType === 'task' ? 'Task ID' : 'Category ID'}</span>
            <input value={form.scopeId} maxLength={64} onChange={(event) => set('scopeId', event.target.value)} />
          </label>
        )}
        <label className="settings-memory-field">
          <span>Strength</span>
          <select
            value={form.strength}
            onChange={(event) => set('strength', event.target.value as PreferenceFormState['strength'])}
          >
            <option value="low">Low</option>
            <option value="medium">Medium</option>
            <option value="strong">Strong</option>
            <option value="hard">Hard constraint</option>
          </select>
        </label>
        {form.kind === 'gap' && (
          <>
            <label className="settings-memory-field">
              <span>Gap minutes</span>
              <input type="number" min="0" max="1440" value={form.gapMinutes} onChange={(event) => set('gapMinutes', event.target.value)} />
            </label>
            <label className="settings-memory-field">
              <span>Minimum task minutes</span>
              <input type="number" min="0" max="1440" value={form.minimumTaskDurationMinutes} onChange={(event) => set('minimumTaskDurationMinutes', event.target.value)} />
            </label>
          </>
        )}
        {form.kind === 'unavailable_window' && (
          <>
            <label className="settings-memory-field">
              <span>Start</span>
              <input type="time" value={form.startTime} onChange={(event) => set('startTime', event.target.value)} />
            </label>
            <label className="settings-memory-field">
              <span>End</span>
              <input type="time" value={form.endTime} onChange={(event) => set('endTime', event.target.value)} />
            </label>
          </>
        )}
        <label className="settings-memory-field">
          <span>Effective from</span>
          <input type="datetime-local" value={form.effectiveFrom} onChange={(event) => set('effectiveFrom', event.target.value)} />
        </label>
        <label className="settings-memory-field">
          <span>Expires</span>
          <input type="datetime-local" value={form.effectiveUntil} onChange={(event) => set('effectiveUntil', event.target.value)} />
        </label>
      </div>
      <div className="settings-memory-editor-actions">
        <button type="button" className="settings-memory-secondary-btn" onClick={onCancel}>
          <X size={15} aria-hidden="true" /> Cancel
        </button>
        <button type="submit" className="settings-memory-primary-btn" disabled={invalid || isSaving}>
          <Save size={15} aria-hidden="true" />
          {isSaving ? 'Saving...' : isEditing ? 'Save changes' : 'Add preference'}
        </button>
      </div>
    </form>
  );
}

export function RememberedPreferencesSettings() {
  const [memory, setMemory] = useState<RememberedPreferencesResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [updatingPreferenceId, setUpdatingPreferenceId] = useState<string | null>(null);
  const [editingPreferenceId, setEditingPreferenceId] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [form, setForm] = useState<PreferenceFormState>(EMPTY_FORM);
  const [query, setQuery] = useState('');
  const [view, setView] = useState<'all' | 'active' | 'issues'>('all');

  const loadMemory = async () => {
    const nextMemory = await fetchRememberedPreferences({ includeInactive: true });
    setMemory(nextMemory);
  };

  useEffect(() => {
    const loadInitialMemory = async () => {
      try {
        const nextMemory = await fetchRememberedPreferences({ includeInactive: true });
        setMemory(nextMemory);
        setError(null);
      } catch (requestError) {
        setError(requestError instanceof Error ? requestError.message : 'Failed to load remembered preferences.');
      } finally {
        setIsLoading(false);
      }
    };
    void loadInitialMemory();
  }, []);

  const closeEditor = () => {
    setShowCreate(false);
    setEditingPreferenceId(null);
    setForm(EMPTY_FORM);
  };

  const handleToggleActive = async (preference: RememberedPreference) => {
    setUpdatingPreferenceId(preference.id);
    try {
      await updateRememberedPreference(preference.id, { active: !preference.active });
      await loadMemory();
      setError(null);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Failed to update remembered preference.');
    } finally {
      setUpdatingPreferenceId(null);
    }
  };

  const handleSave = async () => {
    setUpdatingPreferenceId(editingPreferenceId ?? 'new');
    try {
      const payload = formPayload(form);
      if (editingPreferenceId) {
        const current = memory?.preferences.find((item) => item.id === editingPreferenceId);
        if (!current) throw new Error('The preference is no longer available.');
        await updateRememberedPreference(editingPreferenceId, { ...payload, revision: current.revision });
      } else {
        await createRememberedPreference(payload);
      }
      await loadMemory();
      closeEditor();
      setError(null);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Failed to save remembered preference.');
    } finally {
      setUpdatingPreferenceId(null);
    }
  };

  const handleDelete = async (preference: RememberedPreference) => {
    if (!window.confirm(`Delete "${preferenceTitle(preference)}" permanently? This cannot be undone.`)) return;
    setUpdatingPreferenceId(preference.id);
    try {
      await deleteRememberedPreference(preference.id);
      await loadMemory();
      if (editingPreferenceId === preference.id) closeEditor();
      setError(null);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Failed to delete remembered preference.');
    } finally {
      setUpdatingPreferenceId(null);
    }
  };

  const preferences = useMemo(() => memory?.preferences ?? [], [memory]);
  const filteredPreferences = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase();
    return preferences.filter((preference) => {
      if (view === 'active' && !preference.active) return false;
      if (view === 'issues' && !preference.issue) return false;
      if (!normalized) return true;
      return [preferenceTitle(preference), preferenceDetail(preference), preference.domain]
        .join(' ')
        .toLocaleLowerCase()
        .includes(normalized);
    });
  }, [preferences, query, view]);

  return (
    <section className="settings-auth-card settings-memory-card">
      <div className="settings-provider-header">
        <div>
          <p className="settings-auth-title">Remembered preferences</p>
          <p className="settings-auth-copy">Preferences saved from approved AI updates and settings.</p>
        </div>
        <div className="settings-memory-header-actions">
          <span className="settings-auth-pill">{memory ? `${memory.summary.activeCount} active` : 'Loading'}</span>
          <button
            type="button"
            className="settings-memory-icon-btn is-primary"
            aria-label="Add preference"
            title="Add preference"
            onClick={() => {
              setEditingPreferenceId(null);
              setForm(EMPTY_FORM);
              setShowCreate(true);
            }}
          >
            <Plus size={16} aria-hidden="true" />
          </button>
        </div>
      </div>

      {error && <div className="settings-auth-error" role="alert">{error}</div>}
      {showCreate && (
        <PreferenceEditor form={form} onChange={setForm} onCancel={closeEditor} onSubmit={() => void handleSave()} isSaving={updatingPreferenceId === 'new'} isEditing={false} />
      )}

      <div className="settings-memory-toolbar">
        <label className="settings-memory-search">
          <Search size={15} aria-hidden="true" />
          <input aria-label="Search preferences" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search" />
        </label>
        <div className="settings-memory-view" role="group" aria-label="Preference filter">
          {(['all', 'active', 'issues'] as const).map((item) => (
            <button key={item} type="button" className={view === item ? 'is-active' : ''} onClick={() => setView(item)}>
              {labelFromToken(item)}
            </button>
          ))}
        </div>
      </div>

      {isLoading && <div className="settings-memory-empty" role="status">Loading remembered preferences...</div>}
      {!isLoading && preferences.length === 0 && <div className="settings-memory-empty">No remembered preferences yet.</div>}
      {!isLoading && preferences.length > 0 && filteredPreferences.length === 0 && <div className="settings-memory-empty">No matching preferences.</div>}

      {filteredPreferences.length > 0 && (
        <div className="settings-memory-list">
          {filteredPreferences.map((preference) => (
            <div key={preference.id}>
              <article className={`settings-memory-item${preference.active ? '' : ' is-disabled'}${preference.issue ? ' has-issue' : ''}`}>
                <div className="settings-memory-main">
                  <div className="settings-memory-heading-row">
                    <strong>{preferenceTitle(preference)}</strong>
                    <span className={`settings-memory-status${preference.active ? '' : ' disabled'}`}>{preference.active ? 'Active' : 'Disabled'}</span>
                  </div>
                  <p>{preferenceDetail(preference)}</p>
                  {preference.issue && (
                    <div className="settings-memory-issue" role="status">
                      <AlertTriangle size={15} aria-hidden="true" />
                      <span>{preference.issue.reason}</span>
                    </div>
                  )}
                  <div className="settings-memory-meta">
                    <span>{labelFromToken(preference.domain)}</span>
                    <span>{labelFromToken(preference.strength)}</span>
                    <span>{labelFromToken(preference.scopeType)}</span>
                    <span>{formatDateTime(preference.updatedAt)}</span>
                  </div>
                </div>
                <div className="settings-memory-actions">
                  <button type="button" className="settings-memory-icon-btn" aria-label={`Edit ${preferenceTitle(preference)}`} title="Edit" onClick={() => {
                    setShowCreate(false);
                    setEditingPreferenceId(preference.id);
                    setForm(formFromPreference(preference));
                  }}>
                    <Pencil size={15} aria-hidden="true" />
                  </button>
                  <button type="button" className="settings-memory-icon-btn" aria-label={preference.active ? 'Disable' : 'Restore'} title={preference.active ? 'Disable' : 'Restore'} onClick={() => void handleToggleActive(preference)} disabled={updatingPreferenceId === preference.id}>
                    {preference.active ? <Power size={15} aria-hidden="true" /> : <RotateCcw size={15} aria-hidden="true" />}
                  </button>
                  <button type="button" className="settings-memory-icon-btn is-delete" aria-label="Delete" title="Delete" onClick={() => void handleDelete(preference)} disabled={updatingPreferenceId === preference.id}>
                    <Trash2 size={15} aria-hidden="true" />
                  </button>
                </div>
              </article>
              {editingPreferenceId === preference.id && (
                <PreferenceEditor form={form} onChange={setForm} onCancel={closeEditor} onSubmit={() => void handleSave()} isSaving={updatingPreferenceId === preference.id} isEditing />
              )}
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
