import * as React from "react";
import { ChevronDown } from "lucide-react";
import { cn } from "@/lib/utils";

/** Native select styled like the shadcn trigger (no popover machinery needed here). */
function Select({ className, children, ...props }: React.ComponentProps<"select">) {
  return (
    <span className={cn("relative inline-flex", className)}>
      <select
        data-slot="select"
        className="border-input hover:bg-accent/50 focus-visible:border-ring focus-visible:ring-ring/50 h-8 w-full appearance-none rounded-md border bg-transparent pr-7 pl-2.5 text-sm transition-colors outline-none focus-visible:ring-[3px] disabled:opacity-50"
        {...props}
      >
        {children}
      </select>
      <ChevronDown className="pointer-events-none absolute top-1/2 right-2 size-3.5 -translate-y-1/2 opacity-50" />
    </span>
  );
}

export { Select };