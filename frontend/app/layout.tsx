import type { ReactNode } from "react";

export const metadata = { title: "Trading Engine — Sistem" };

export default function RootLayout({ children }: { children: ReactNode }) {
  return <html lang="id"><body>{children}</body></html>;
}
