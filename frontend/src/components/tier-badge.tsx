import { cn } from "cn";

/** Mirrors the risk tiers in `app/tools/registry.py`. */
export type RiskTier = "READ" | "LOW" | "HIGH";

const TIER_STYLES: Record<RiskTier, string> = {
  READ: "bg-tier-read text-tier-read-foreground",
  LOW: "bg-tier-low text-tier-low-foreground",
  HIGH: "bg-tier-high text-tier-high-foreground",
};

const TIER_TITLES: Record<RiskTier, string> = {
  READ: "Read-only. Runs automatically.",
  LOW: "Has side effects. Runs automatically only if its feature flag is on.",
  HIGH: "Always requires human approval before it runs.",
};

/**
 * A tool's risk tier.
 *
 * Its own component rather than a `Badge` variant because the tier appears in
 * three places — the chat timeline, the approvals inbox and the audit view —
 * and must look identical in all of them. The colours come from semantic
 * tokens in index.css, kept separate from the accent hue: tier is severity,
 * not branding.
 */
export function TierBadge({ tier, className }: { tier: RiskTier; className?: string }) {
  return (
    <span
      title={TIER_TITLES[tier]}
      className={cn(
        "inline-flex items-center rounded-sm px-1.5 py-0.5 font-mono text-[10px] font-semibold tracking-wider",
        TIER_STYLES[tier],
        className,
      )}
    >
      {tier}
    </span>
  );
}
