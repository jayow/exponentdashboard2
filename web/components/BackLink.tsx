'use client';
import Link from 'next/link';
import { useRouter } from 'next/navigation';

/**
 * Back link that uses real browser history (router.back) when there's a page to
 * return to — so the previous page's scroll position and tab state are restored
 * instead of jumping to the top of a fresh navigation. Falls back to `fallback`
 * (a normal forward nav) when the detail page was opened directly / in a fresh
 * tab, and for no-JS + accessibility the underlying <Link href> still works.
 */
export function BackLink({
  label,
  fallback = '/',
  className,
}: {
  label: string;
  fallback?: string;
  className?: string;
}) {
  const router = useRouter();
  return (
    <Link
      href={fallback}
      className={className}
      onClick={(e) => {
        if (typeof window !== 'undefined' && window.history.length > 1) {
          e.preventDefault();
          router.back();
        }
      }}
    >
      {label}
    </Link>
  );
}
