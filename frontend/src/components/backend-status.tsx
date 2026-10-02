import { useQuery } from "@tanstack/react-query";

import { apiGet, type Health } from "@/lib/api";
import { cn } from "cn";

/**
 * Live backend connection indicator, shown in the sidebar footer.
 *
 * This is deliberately real rather than decorative: it exercises the whole
 * chain — TanStack Query, the fetch wrapper, the Vite proxy and the FastAPI
 * `/health` endpoint — so the shell proves its own wiring on first load
 * instead of looking finished while connected to nothing.
 */
export function BackendStatus() {
  const { data, isPending, isError } = useQuery({
    queryKey: ["health"],
    queryFn: ({ signal }) => apiGet<Health>("/health", signal),
    // The backend restarting is normal in development; notice within 15s.
    refetchInterval: 15_000,
  });

  const state = isPending ? "checking" : isError ? "offline" : "online";

  const label =
    state === "online" ? `v${data?.version} · ${data?.env}` : state === "offline" ? "offline" : "…";

  return (
    <div className="flex items-center gap-2 px-2 py-1.5 text-xs text-sidebar-foreground/70">
      <span
        aria-hidden
        className={cn(
          "size-2 shrink-0 rounded-full",
          state === "online" && "bg-emerald-500",
          state === "offline" && "bg-destructive",
          state === "checking" && "animate-pulse bg-muted-foreground",
        )}
      />
      <span className="truncate">
        <span className="font-medium">Backend</span> {label}
      </span>
    </div>
  );
}
