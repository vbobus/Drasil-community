import { createContext, useContext } from 'react';
import type { DrasilAccount } from '../../api/connection';

export interface AccountContextValue {
  account: DrasilAccount;
  backendLabel: string;
  openAccountSwitcher: () => void;
  signOut: () => Promise<void>;
}

export const AccountContext = createContext<AccountContextValue | null>(null);

export function useDrasilAccount(): AccountContextValue {
  const value = useContext(AccountContext);
  if (value === null) {
    throw new Error('useDrasilAccount must be used inside AccountBoundary.');
  }
  return value;
}
