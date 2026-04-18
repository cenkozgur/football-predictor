import Link from "next/link";

export default function HomePage() {
  return (
    <section className="flex flex-col items-center justify-center min-h-[70vh] text-center px-2">
      <div className="w-16 h-16 rounded-2xl bg-accent-dim flex items-center justify-center mb-6">
        <svg className="w-8 h-8 text-accent" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M7.5 14.25v2.25m3-4.5v4.5m3-6.75v6.75m3-9v9M6 20.25h12A2.25 2.25 0 0 0 20.25 18V6A2.25 2.25 0 0 0 18 3.75H6A2.25 2.25 0 0 0 3.75 6v12A2.25 2.25 0 0 0 6 20.25Z" />
        </svg>
      </div>
      <h1 className="text-3xl font-bold tracking-tight">
        Tahmin
      </h1>
      <p className="mt-3 text-muted text-sm leading-relaxed max-w-sm">
        Avrupa liglerindeki yaklasan maclar icin Dixon-Coles modelinden
        turetilmis 1X2, alt/ust, KG, kesin skor ve daha fazlasi.
      </p>
      <Link
        href="/matches"
        className="mt-8 inline-flex items-center gap-2 rounded-full bg-accent px-6 py-3 text-sm font-semibold text-black shadow-lg shadow-accent/20 active:scale-95 transition-transform"
      >
        Maclari Gor
        <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M13.5 4.5 21 12m0 0-7.5 7.5M21 12H3" />
        </svg>
      </Link>
    </section>
  );
}
