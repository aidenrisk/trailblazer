// Writes last-run.json and prints no RRSTATUS line.
// Proves the file is read on its own, and that the absent line is warned about.
const fs = require('fs');
const path = require('path');

const METADATA = 'onboarding-pie-gl.metadata.json';
const QUESTIONS = 'onboarding-pie-gl.questions.json';

const status = {
  outcome: 'quote',
  reachedQuote: true,
  stoppedReason: null,
  premium: 1500,
  premiumDisplay: '$1,500/yr',
  quoteNumber: 'Q-FILE-1',
  quoteUrl: 'https://portal.example.com/quote/file-1'
};

fs.writeFileSync(path.join(__dirname, 'last-run.json'), JSON.stringify(status));
process.exit(0);
