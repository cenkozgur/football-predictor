import type { Metadata, Viewport } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import Link from "next/link";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "Tahmin",
  description: "Avrupa futbolu tahminleri — her mac, her market.",
  manifest: "/manifest.json",
  appleWebApp: {
    capable: true,
    statusBarStyle: "black-translucent",
    title: "Tahmin",
  },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  maximumScale: 1,
  userScalable: false,
  themeColor: "#09090b",
  viewportFit: "cover",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="tr"
      className={`${geistSans.variable} ${geistMono.variable} h-full`}
    >
      <head>
        <link rel="apple-touch-icon" href="/icon-192.png" />
      </head>
      <body className="min-h-full flex flex-col">
        {/* Bottom nav on mobile, top nav on desktop */}
        <header className="hidden md:block border-b border-card-border bg-card safe-top">
          <div className="mx-auto max-w-2xl px-4 py-3 flex items-center justify-between">
            <Link href="/" className="text-accent font-bold text-lg tracking-tight">
              Tahmin
            </Link>
            <nav className="flex gap-5 text-sm text-muted">
              <Link href="/matches" className="hover:text-foreground transition-colors">
                Maclar
              </Link>
              <Link href="/kuponlar" className="hover:text-foreground transition-colors">
                Kuponlar
              </Link>
            </nav>
          </div>
        </header>

        <main className="flex-1 mx-auto w-full max-w-2xl px-4 pt-4 pb-24 md:pb-8">
          {children}
        </main>

        {/* Mobile bottom tab bar */}
        <nav className="md:hidden fixed bottom-0 inset-x-0 bg-card/95 backdrop-blur-lg border-t border-card-border safe-bottom z-50">
          <div className="flex items-center justify-around h-14 max-w-2xl mx-auto">
            <Link
              href="/matches"
              className="flex flex-col items-center gap-0.5 text-muted hover:text-accent transition-colors"
            >
              <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 12h16.5m-16.5 3.75h16.5M3.75 19.5h16.5M5.625 4.5h12.75a1.875 1.875 0 0 1 0 3.75H5.625a1.875 1.875 0 0 1 0-3.75Z" />
              </svg>
              <span className="text-[10px] font-medium">Maclar</span>
            </Link>
            <Link
              href="/kuponlar"
              className="flex flex-col items-center gap-0.5 text-muted hover:text-accent transition-colors"
            >
              <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M2.25 8.25h19.5M2.25 9h19.5m-16.5 5.25h6m-6 2.25h3m-3.75 3h15a2.25 2.25 0 0 0 2.25-2.25V6.75A2.25 2.25 0 0 0 19.5 4.5h-15a2.25 2.25 0 0 0-2.25 2.25v10.5A2.25 2.25 0 0 0 4.5 19.5Z" />
              </svg>
              <span className="text-[10px] font-medium">Kuponlar</span>
            </Link>
            <Link
              href="/"
              className="flex flex-col items-center gap-0.5 text-muted hover:text-accent transition-colors"
            >
              <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M7.5 14.25v2.25m3-4.5v4.5m3-6.75v6.75m3-9v9M6 20.25h12A2.25 2.25 0 0 0 20.25 18V6A2.25 2.25 0 0 0 18 3.75H6A2.25 2.25 0 0 0 3.75 6v12A2.25 2.25 0 0 0 6 20.25Z" />
              </svg>
              <span className="text-[10px] font-medium">Tahmin</span>
            </Link>
          </div>
        </nav>
      </body>
    </html>
  );
}
