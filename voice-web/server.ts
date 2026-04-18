import { createServer } from 'node:http';
import { parse } from 'node:url';
import next from 'next';

const dev = process.env.NODE_ENV !== 'production';
const port = parseInt(process.env.PORT || '13036', 10);

const app = next({ dev, hostname: 'localhost', port });
const handle = app.getRequestHandler();

app.prepare().then(() => {
  const server = createServer((req, res) => {
    const parsedUrl = parse(req.url!, true);
    handle(req, res, parsedUrl);
  });

  server.listen(port, () => {
    console.log(`> Next.js ready on http://localhost:${port}`);
  });

  const shutdown = () => {
    console.log('\n[server] Shutting down...');
    server.close(() => process.exit(0));
    setTimeout(() => process.exit(1), 3000);
  };
  process.on('SIGTERM', shutdown);
  process.on('SIGINT', shutdown);
});
