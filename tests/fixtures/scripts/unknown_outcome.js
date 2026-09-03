// Reports an outcome outside the closed vocabulary while having reached a
// quote. It collapses to `quote` because `reachedQuote` is true.
const fs = require('fs');
const path = require('path');

const METADATA = 'onboarding-pie-gl.metadata.json';
const QUESTIONS = 'onboarding-pie-gl.questions.json';

const status = {
  outcome: 'referred-to-underwriting',
  reachedQuote: true,
  premium: 900
};

fs.writeFileSync(path.join(__dirname, 'last-run.json'), JSON.stringify(status));
console.log('RRSTATUS ' + JSON.stringify(status));
process.exit(0);
