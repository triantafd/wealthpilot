import type { ReactNode } from "react";

import { Separator } from "@/components/ui/separator";
import { SidebarTrigger } from "@/components/ui/sidebar";
import { ThemeToggle } from "@/components/theme-toggle";

/** Page header: sidebar trigger, title, optional actions. */
export function PageHeader({
  title,
  description,
  actions,
}: {
  title: string;
  description?: string;
  actions?: ReactNode;
}) {
  return (
    <header className="flex h-14 shrink-0 items-center gap-2 border-b px-4">
      <SidebarTrigger className="-ml-1" />
      <Separator orientation="vertical" className="mr-1 h-4" />
      <div className="min-w-0 flex-1">
        <h1 className="truncate text-sm font-semibold">{title}</h1>
        {description ? (
          <p className="truncate text-xs text-muted-foreground">{description}</p>
        ) : null}
      </div>
      <div className="flex items-center gap-1">
        {actions}
        <ThemeToggle />
      </div>
    </header>
  );
}

/**
 * Placeholder for a screen whose backend does not exist yet.
 *
 * It names the phase that wires it up rather than showing invented data. A
 * dashboard full of plausible fake numbers reads as finished and then has to be
 * unpicked; stating what is missing is more useful while the backend is being
 * built.
 */
export function NotWiredYet({
  phase,
  will,
  needs,
}: {
  phase: string;
  will: string[];
  needs: string;
}) {
  return (
    <div className="mx-auto w-full max-w-xl px-4 py-12">
      <div className="rounded-lg border border-dashed p-6">
        <p className="font-mono text-[10px] font-semibold tracking-wider text-muted-foreground uppercase">
          {phase}
        </p>
        <h2 className="mt-2 text-sm font-semibold">This screen is not wired up yet</h2>
        <p className="mt-1 text-sm text-muted-foreground">
          The shell and routing are in place. It will show:
        </p>
        <ul className="mt-3 space-y-1.5">
          {will.map((item) => (
            <li key={item} className="flex gap-2 text-sm">
              <span aria-hidden className="mt-2 size-1 shrink-0 rounded-full bg-primary" />
              <span>{item}</span>
            </li>
          ))}
        </ul>
        <p className="mt-4 border-t pt-3 text-xs text-muted-foreground">
          Waiting on: <span className="font-medium text-foreground">{needs}</span>
        </p>
      </div>
    </div>
  );
}
