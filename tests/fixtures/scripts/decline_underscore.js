// Reports `appetite_decline` -- the underscore spelling the runner normalizes.
// A decline is a successful run, so this exits 0.
const fs = require('fs');
const path = require('path');

const METADATA = 'onboarding-pie-gl.metadata.json';
const QUESTIONS = 'onboarding-pie-gl.questions.json';

const status = {
  outcome: 'appetite_decline',
  reachedQuote: false,
  stoppedReason: 'Class code 8810 is outside appetite in TX'
};

fs.writeFileSync(path.join(__dirname, 'last-run.json'), JSON.stringify(status));
console.log('RRSTATUS ' + JSON.stringify(status));
process.exit(0);
