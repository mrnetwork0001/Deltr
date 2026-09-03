// Landing page at "/". The dashboard lives at /app/. Everything here is static;
// the only network call is the hero's health ping (client component).
import Nav from "@/components/landing/Nav";
import Hero from "@/components/landing/Hero";
import HowItWorks from "@/components/landing/HowItWorks";
import ArchitectureDiagram from "@/components/landing/ArchitectureDiagram";
import EdgeExplainer from "@/components/landing/EdgeExplainer";
import SafetyTable from "@/components/landing/SafetyTable";
import ConnectPanel from "@/components/landing/ConnectPanel";
import AgentOsPanel from "@/components/landing/AgentOsPanel";
import DemoTimeline from "@/components/landing/DemoTimeline";
import Hackathon from "@/components/landing/Hackathon";
import Faq from "@/components/landing/Faq";
import Footer from "@/components/landing/Footer";

export default function LandingPage() {
  return (
    <>
      <style>{`html{scroll-behavior:smooth;scroll-padding-top:4.5rem}`}</style>
      <Nav />
      <main className="overflow-x-hidden">
        <Hero />
        <HowItWorks />
        <ArchitectureDiagram />
        <EdgeExplainer />
        <SafetyTable />
        <ConnectPanel />
        <AgentOsPanel />
        <DemoTimeline />
        <Hackathon />
        <Faq />
      </main>
      <Footer />
    </>
  );
}
