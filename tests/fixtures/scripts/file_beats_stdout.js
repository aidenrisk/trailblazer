// Writes one outcome to last-run.json and prints a different one on stdout.
// Only used to prove which source wins; a real script writes the same object
// to both.
const fs = require('fs');
const path = require('path');

const METADATA = 'onboarding-pie-gl.metadata.json';
const QUESTIONS = 'onboarding-pie-gl.questions.json';

const fromFile = { outcome: 'quote', reachedQuote: true, quoteNumber: 'Q-FROM-FILE' };
const fromStdout = { outcome: 'stuck', reachedQuote: false, stoppedReason: 'from stdout' };

fs.writeFileSync(path.join(__dirname, 'last-run.json'), JSON.stringify(fromFile));
console.log('RRSTATUS ' + JSON.stringify(fromStdout));
process.exit(0);
