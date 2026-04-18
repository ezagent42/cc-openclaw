'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { VoiceClient } from '@/lib/voice-client';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Card, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Phone, PhoneOff } from 'lucide-react';

interface TranscriptEntry {
  role: 'user' | 'bot';
  text: string;
  isInterim: boolean;
  timestamp: number;
}

const DEFAULT_SYSTEM_ROLE = '你是OpenClaw智能助手。当用户询问商品信息时，请基于提供的知识回答。如果没有相关知识，请如实告知。保持简洁友好。';
const DEFAULT_GREETING = '你好，请问有什么可以帮你？';
const DEFAULT_COMFORT_TEXT = '稍等，我帮你查一下。';

const PRODUCTS = [
  { name: '商品1', price: '100元', note: '库存充足' },
  { name: '商品2', price: '200元', note: '库存充足' },
  { name: '商品3', price: '300元', note: '限时优惠' },
  { name: '商品4', price: '400元', note: '需要预订' },
  { name: '商品5', price: '500元', note: '新品上市' },
];

export default function Home() {
  const [tab, setTab] = useState('config');
  const [state, setState] = useState('idle');
  const [transcripts, setTranscripts] = useState<TranscriptEntry[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [systemRole, setSystemRole] = useState(DEFAULT_SYSTEM_ROLE);
  const [greeting, setGreeting] = useState(DEFAULT_GREETING);
  const [comfortText, setComfortText] = useState(DEFAULT_COMFORT_TEXT);
  const [mode, setMode] = useState<'e2e' | 'split'>('e2e');
  const clientRef = useRef<VoiceClient | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' });
  }, [transcripts]);

  const handleTranscript = useCallback((role: 'user' | 'bot', text: string, interim: boolean) => {
    setTranscripts((prev) => {
      for (let i = prev.length - 1; i >= 0; i--) {
        if (prev[i].role === role) {
          if (prev[i].isInterim) {
            const updated = [...prev];
            updated[i] = { role, text, isInterim: interim, timestamp: Date.now() };
            return updated;
          }
          break;
        }
      }
      return [...prev, { role, text, isInterim: interim, timestamp: Date.now() }];
    });
  }, []);

  const handleStart = useCallback(() => {
    setError(null);
    setTranscripts([]);
    setTab('call');
    const client = new VoiceClient();
    clientRef.current = client;

    client.onState = (s) => {
      setState(s);
      if (s === 'idle') setTab('config');
    };
    client.onTranscript = handleTranscript;
    client.onError = (msg) => setError(msg);

    client.start({ systemRole, greeting, comfortText, mode });
  }, [handleTranscript, systemRole, greeting, comfortText, mode]);

  const handleStop = useCallback(() => {
    clientRef.current?.stop();
    clientRef.current = null;
  }, []);

  const isActive = state === 'connecting' || state === 'greeting' || state === 'talking' || state === 'ending';

  return (
    <div className="flex flex-col h-screen bg-background">
      <Tabs value={tab} onValueChange={(v) => !isActive && setTab(v)} className="flex flex-col flex-1">
        <TabsList className="w-full h-10 rounded-none border-b bg-muted/50">
          <TabsTrigger value="config" disabled={isActive}>
            Configuration
          </TabsTrigger>
          <TabsTrigger value="call">
            Call
            {isActive && (
              <Badge variant="secondary" className="ml-2 text-xs">
                {state}
              </Badge>
            )}
          </TabsTrigger>
        </TabsList>

        <TabsContent value="config" className="flex-1 flex flex-col mt-0">
          <div className="flex-1 overflow-y-auto p-6">
            <div className="max-w-2xl mx-auto space-y-6">
              <div className="space-y-2">
                <Label htmlFor="system-role">System Prompt</Label>
                <Textarea
                  id="system-role"
                  value={systemRole}
                  onChange={(e) => setSystemRole(e.target.value)}
                  disabled={isActive}
                  rows={4}
                  placeholder="系统提示词..."
                />
              </div>

              <div className="space-y-2">
                <Label htmlFor="greeting">Greeting (开场白)</Label>
                <Input
                  id="greeting"
                  value={greeting}
                  onChange={(e) => setGreeting(e.target.value)}
                  disabled={isActive}
                  placeholder="开场白文本..."
                />
              </div>

              <div className="space-y-2">
                <Label htmlFor="comfort">Comfort Text (安抚语)</Label>
                <Input
                  id="comfort"
                  value={comfortText}
                  onChange={(e) => setComfortText(e.target.value)}
                  disabled={isActive}
                  placeholder="RAG 查询时播放的安抚语..."
                />
              </div>

              <div className="space-y-2">
                <Label>Mode</Label>
                <div className="flex gap-4">
                  <label className="flex items-center gap-2 text-sm cursor-pointer">
                    <input type="radio" name="mode" value="e2e" checked={mode === 'e2e'}
                      onChange={() => setMode('e2e')} disabled={isActive}
                      className="accent-primary" />
                    E2E (端到端语音对话)
                  </label>
                  <label className="flex items-center gap-2 text-sm cursor-pointer">
                    <input type="radio" name="mode" value="split" checked={mode === 'split'}
                      onChange={() => setMode('split')} disabled={isActive}
                      className="accent-primary" />
                    Split (ASR + LLM + TTS)
                  </label>
                </div>
              </div>

              <div className="space-y-2">
                <Label>可查询商品 (说任意内容触发 RAG)</Label>
                <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-5 gap-3">
                  {PRODUCTS.map((p) => (
                    <Card key={p.name}>
                      <CardContent className="p-3">
                        <div className="font-medium text-sm">{p.name}</div>
                        <div className="text-sm text-muted-foreground">{p.price}</div>
                        <Badge variant="outline" className="mt-1 text-xs font-normal">
                          {p.note}
                        </Badge>
                      </CardContent>
                    </Card>
                  ))}
                </div>
              </div>
            </div>
          </div>

          <div className="border-t p-4 flex justify-center">
            <Button size="lg" onClick={handleStart} disabled={isActive}>
              <Phone className="mr-2 h-4 w-4" />
              Start Call
            </Button>
          </div>
        </TabsContent>

        <TabsContent value="call" className="flex-1 flex flex-col mt-0">
          <div ref={scrollRef} className="flex-1 overflow-y-auto p-6 space-y-3">
            {transcripts.map((t, i) => (
              <div
                key={i}
                className={`max-w-[80%] px-4 py-2.5 rounded-2xl text-sm ${
                  t.role === 'user'
                    ? 'ml-auto bg-primary text-primary-foreground'
                    : 'mr-auto bg-muted text-foreground'
                } ${t.isInterim ? 'opacity-50' : ''}`}
              >
                {t.text}
              </div>
            ))}
            {state === 'connecting' && (
              <p className="text-center text-sm text-muted-foreground">Connecting...</p>
            )}
            {state === 'greeting' && (
              <p className="text-center text-sm text-muted-foreground">Bot is greeting...</p>
            )}
            {!isActive && transcripts.length === 0 && (
              <p className="text-center text-sm text-muted-foreground">
                No active call. Go to Configuration to start.
              </p>
            )}
          </div>

          {error && (
            <div className="mx-6 mb-3 px-4 py-2 bg-destructive/10 text-destructive text-sm rounded-lg">
              {error}
            </div>
          )}

          {isActive && (
            <div className="border-t p-4 flex justify-center">
              <Button size="lg" variant="destructive" onClick={handleStop} disabled={state === 'ending'}>
                <PhoneOff className="mr-2 h-4 w-4" />
                {state === 'ending' ? 'Ending...' : 'End Call'}
              </Button>
            </div>
          )}
        </TabsContent>
      </Tabs>
    </div>
  );
}
