// An unrecognized outcome with no quote reached. It collapses to `stuck`.
const fs = require('fs');
const path = require('path');

const METADATA = 'onboarding-pie-gl.metadata.json';
const QUESTIONS = 'onboarding-pie-gl.questions.json';

const status = { outcome: 'timed-out-waiting', reachedQuote: false };

fs.writeFileSync(path.join(__dirname, 'last-run.json'), JSON.stringify(status));
console.log('RRSTATUS ' + JSON.stringify(status));
process.exit(1);
