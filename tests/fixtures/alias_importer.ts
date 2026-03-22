import { cn } from '@/lib/utils';
import { UserService } from './sample_typescript';

export function formatUser(name: string): string {
  return cn('user', name);
}
