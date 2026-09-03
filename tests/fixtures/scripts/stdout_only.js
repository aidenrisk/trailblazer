// Prints RRSTATUS and writes no last-run.json.
// The stdout fallback on its own, with log noise around the line.
const METADATA = 'onboarding-pie-gl.metadata.json';
const QUESTIONS = 'onboarding-pie-gl.questions.json';

const status = {
  outcome: 'quote',
  reachedQuote: true,
  premium: '$2,400',
  quoteNumber: 'Q-STDOUT-1'
};

console.log('navigating to stage business-info');
console.log('RRSTATUS ' + JSON.stringify(status));
console.log('teardown complete');
process.exit(0);
