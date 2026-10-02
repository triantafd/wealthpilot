import { BarChart3, FileText, FlaskConical, MessageSquare, ShieldCheck } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { NavLink } from "react-router-dom";

import { BackendStatus } from "@/components/backend-status";
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarRail,
} from "@/components/ui/sidebar";

interface NavItem {
  to: string;
  label: string;
  icon: LucideIcon;
}

/**
 * The five screens in ARCHITECTURE section 7, grouped by what the user is
 * doing rather than listed flat: two are for working with the assistant, three
 * are for inspecting how it behaves.
 */
const WORK: readonly NavItem[] = [
  { to: "/chat", label: "Chat", icon: MessageSquare },
  { to: "/approvals", label: "Approvals", icon: ShieldCheck },
];

const INSPECT: readonly NavItem[] = [
  { to: "/documents", label: "Documents", icon: FileText },
  { to: "/usage", label: "Usage", icon: BarChart3 },
  { to: "/evals", label: "Evals", icon: FlaskConical },
];

function NavSection({ label, items }: { label: string; items: readonly NavItem[] }) {
  return (
    <SidebarGroup>
      <SidebarGroupLabel>{label}</SidebarGroupLabel>
      <SidebarGroupContent>
        <SidebarMenu>
          {items.map(({ to, label: itemLabel, icon: Icon }) => (
            <SidebarMenuItem key={to}>
              {/* NavLink gives the active state; asChild lets the shadcn button
                  style the link itself rather than wrapping it. */}
              <NavLink to={to}>
                {({ isActive }) => (
                  <SidebarMenuButton asChild isActive={isActive} tooltip={itemLabel}>
                    <span>
                      <Icon className="size-4" />
                      <span>{itemLabel}</span>
                    </span>
                  </SidebarMenuButton>
                )}
              </NavLink>
            </SidebarMenuItem>
          ))}
        </SidebarMenu>
      </SidebarGroupContent>
    </SidebarGroup>
  );
}

export function AppSidebar() {
  return (
    <Sidebar collapsible="icon">
      <SidebarHeader>
        <div className="flex items-center gap-2 px-2 py-1.5">
          <div
            aria-hidden
            className="flex size-7 shrink-0 items-center justify-center rounded-md bg-primary font-mono text-xs font-bold text-primary-foreground"
          >
            WP
          </div>
          <div className="min-w-0 group-data-[collapsible=icon]:hidden">
            <p className="truncate text-sm font-semibold">WealthPilot</p>
            <p className="truncate text-xs text-sidebar-foreground/60">Adviser assistant</p>
          </div>
        </div>
      </SidebarHeader>

      <SidebarContent>
        {/* shadcn's Sidebar renders plain divs, so the nav landmark is ours to
            add. Without it a screen reader has no way to jump to navigation. */}
        <nav aria-label="Main">
          <NavSection label="Assist" items={WORK} />
          <NavSection label="Inspect" items={INSPECT} />
        </nav>
      </SidebarContent>

      <SidebarFooter className="group-data-[collapsible=icon]:hidden">
        <BackendStatus />
      </SidebarFooter>

      <SidebarRail />
    </Sidebar>
  );
}
