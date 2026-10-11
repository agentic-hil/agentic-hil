// hq180: what Serial.write() and Serial.flush() do when the UART's own
// interrupt cannot run, on a NUCLEO-F446RE under STM32duino.
//
// Upstream stm32duino/Arduino_Core_STM32 issue #1834 says both calls loop
// forever when they are made from an ISR or with interrupts disabled while a
// transmission is in progress. This image runs 17 cases, 20 repetitions each,
// one case per boot: write() with the 64-byte TX ring full, flush(), flush(20)
// and flush(1), each in thread mode with interrupts enabled, in thread mode
// with PRIMASK set, inside a TIM3 update ISR at preemption priority 1 (the
// UART's own level) and inside one at priority 0 (which blocks SysTick too).
//
// The image only measures. It prints DWT cycle counts across the call, PRIMASK,
// BASEPRI and IPSR, the ring's head, tail and transfer size, the USART status
// and control registers, the reset flags and the clock configuration it
// measured with, and it prints no verdict: tests/hq180 recomputes the baud-rate
// divider, the frame time and every deadline from those values and judges the
// bytes against what the bench captured on USART2.
//
// A call that never returns is ended by the independent watchdog. Before every
// call the image stores what the call starts from in .noinit RAM, which a reset
// leaves alone, and prints it after the reset: no debugger reads the program
// counter at a hang, so these breadcrumbs take its place.
#include <Arduino.h>
#include <HardwareTimer.h>
#include <IWatchdog.h>
#include <stddef.h>
#include <stdio.h>

#define TAG "H180"

#define REPS        20U  // repetitions per case
#define WDG_REPS    2U   // repetitions of the watchdog control, before the cases
#define NCASES      17U

// 250 ms nominal: IWatchdog turns that into prescaler /4 and reload 1999, which
// is 8000 LSI cycles, so 170 ms at the fastest LSI the F446 datasheet allows and
// 471 ms at the slowest. Even the fastest is eight times the longest call this
// image makes (flush(20)), so the watchdog never cuts a legitimate call short.
#define IWDG_PERIOD_US  250000UL

// RCC_CSR, by bit position, so that no device header's choice of name matters.
#define CSR_RMVF     (1UL << 24)
#define CSR_SFTRSTF  (1UL << 28)
#define CSR_IWDGRSTF (1UL << 29)

// What the measured call is in a case.
#define STEP_NONE    0U  // armed before the image's own reset
#define STEP_WDGCTL  1U  // armed before the watchdog control's spin
#define STEP_WRITE   2U
#define STEP_FLUSH   3U
#define STEP_FLUSHT  4U
#define STEP_FLUSHS  5U

// The context the measured call is made in.
#define CTX_N  0U  // thread mode, interrupts enabled
#define CTX_A  1U  // thread mode, PRIMASK set
#define CTX_B  2U  // a TIM3 update ISR at preemption priority 1
#define CTX_C  3U  // the same ISR at preemption priority 0

struct Row {
  const char *name;
  const char *ctx;
  uint8_t ctx_id;
  uint8_t step_id;
  uint16_t n;    // payload bytes; a write case queues n - 1 and writes the last
  uint16_t tmo;  // flush timeout in milliseconds, 0 where the call takes none
};

static const Row rows[NCASES] = {
  { "ctl-write",   "n", CTX_N, STEP_WRITE,  64,  0 },
  { "ctl-flush",   "n", CTX_N, STEP_FLUSH,  40,  0 },
  { "w-a",         "a", CTX_A, STEP_WRITE,  64,  0 },
  { "w-b",         "b", CTX_B, STEP_WRITE,  64,  0 },
  { "w-c",         "c", CTX_C, STEP_WRITE,  64,  0 },
  { "f-a",         "a", CTX_A, STEP_FLUSH,  40,  0 },
  { "f-b",         "b", CTX_B, STEP_FLUSH,  40,  0 },
  { "f-c",         "c", CTX_C, STEP_FLUSH,  40,  0 },
  { "t-a",         "a", CTX_A, STEP_FLUSHT, 40, 20 },
  { "t-b",         "b", CTX_B, STEP_FLUSHT, 40, 20 },
  { "t-c",         "c", CTX_C, STEP_FLUSHT, 40, 20 },
  { "s-a",         "a", CTX_A, STEP_FLUSHS, 40,  1 },
  { "s-b",         "b", CTX_B, STEP_FLUSHS, 40,  1 },
  { "s-c",         "c", CTX_C, STEP_FLUSHS, 40,  1 },
  { "s-n",         "n", CTX_N, STEP_FLUSHS, 40,  1 },
  { "ctl-write-2", "n", CTX_N, STEP_WRITE,  64,  0 },
  { "ctl-flush-2", "n", CTX_N, STEP_FLUSH,  40,  0 },
};

#define STATE_MAGIC 0x48313830UL  // "H180"

// Everything that has to survive a reset the image causes itself. Every member
// is volatile: the breadcrumbs must really be in memory before the call that
// may never return, and the watchdog control's spin must really store its cycle
// count before the watchdog fires.
struct State {
  volatile uint32_t magic;  // STATE_MAGIC only while a self-caused reset is expected
  volatile uint32_t boot;
  volatile uint32_t case_index;
  volatile uint32_t rep;
  volatile uint32_t wdg_rep;
  volatile uint32_t step;
  volatile uint32_t acc1;
  volatile uint32_t hp;
  volatile uint32_t head0;
  volatile uint32_t tail0;
  volatile uint32_t size0;
  volatile uint32_t sr0;
  volatile uint32_t cr1_0;
  volatile uint32_t pm0;
  volatile uint32_t bp0;
  volatile uint32_t ipsr;
  volatile uint32_t prio_tim;
  volatile uint32_t en0;
  volatile uint32_t cyc0;
  volatile uint32_t check;        // covers every member from boot to cyc0
  volatile uint32_t spin_cycles;  // rewritten during the spin, so not covered
};

static State state __attribute__((section(".noinit")));

// The serial_t behind `Serial`, recovered from its HAL handle the way uart.c's
// own get_serial_obj() does. It gives the TX ring's indices, the transfer size,
// the peripheral and the interrupt line.
static serial_t *serial;

static HardwareTimer *timer;
static volatile bool step_done;

static uint8_t payload[64];
static char text[416];

// What the measured call left behind, read in the context the call was made in
// and printed afterwards in thread mode.
static volatile uint32_t call_cycles;
static volatile uint32_t call_accepted;
static volatile uint32_t after_pm;
static volatile uint32_t after_bp;
static volatile uint32_t after_en;
static volatile uint32_t after_head;
static volatile uint32_t after_tail;
static volatile uint32_t after_size;
static volatile uint32_t after_sr;
static volatile uint32_t after_cr1;

static const char *step_name(uint32_t step_id)
{
  switch (step_id) {
    case STEP_WRITE:
      return "write";
    case STEP_FLUSH:
      return "flush";
    case STEP_FLUSHT:
      return "flusht";
    case STEP_FLUSHS:
      return "flushs";
    case STEP_WDGCTL:
      return "wdgctl";
    default:
      return "none";
  }
}

static uint32_t state_check(void)
{
  const volatile uint32_t *words = (const volatile uint32_t *)&state;
  size_t first = offsetof(State, boot) / sizeof(uint32_t);
  size_t count = offsetof(State, check) / sizeof(uint32_t);
  uint32_t sum = 0x9E3779B9UL;
  size_t index;

  for (index = first; index < count; index++) {
    sum = (sum * 31UL) + words[index];
  }
  return sum;
}

// Mark the state as the one a self-caused reset should be resumed from. Nothing
// but the reset itself, or the call that may hang, comes after this.
static void arm(uint32_t step_id)
{
  state.step = step_id;
  state.check = state_check();
  __DSB();
  state.magic = STATE_MAGIC;
  __DSB();
}

// One record line: `<TAG> <kind> key=value ...`, ended by CR LF.
static void emit(int written)
{
  size_t length;

  if (written < 0) {
    written = 0;
  }
  length = (size_t)written;
  if (length > sizeof(text) - 1) {
    length = sizeof(text) - 1;  // snprintf truncated: print what it did keep
  }
  Serial.write((const uint8_t *)text, length);
  Serial.write((const uint8_t *)"\r\n", 2);
}

// The case's own data: two lines that name the case, the repetition and the
// half, so that every byte of every repetition can be told from every other.
// They start with `d `, never with the tag, so the record parser ignores them.
static void build_payload(uint32_t case_index, uint32_t rep, uint32_t n)
{
  static const char filler[] = "abcdefghijklmnopqrstuvwxyz";
  uint32_t line = n / 2U;
  uint32_t half;
  uint32_t index;
  uint32_t at = 0;

  for (half = 0; half < 2U; half++) {
    // A header of exactly 16 bytes, so that the line is `line` bytes long.
    payload[at++] = 'd';
    payload[at++] = ' ';
    payload[at++] = 'c';
    payload[at++] = '=';
    payload[at++] = (uint8_t)('0' + ((case_index / 10U) % 10U));
    payload[at++] = (uint8_t)('0' + (case_index % 10U));
    payload[at++] = ' ';
    payload[at++] = 'r';
    payload[at++] = '=';
    payload[at++] = (uint8_t)('0' + ((rep / 10U) % 10U));
    payload[at++] = (uint8_t)('0' + (rep % 10U));
    payload[at++] = ' ';
    payload[at++] = 'k';
    payload[at++] = '=';
    payload[at++] = (uint8_t)('0' + half);
    payload[at++] = ' ';
    for (index = 0; index < (line - 18U); index++) {
      payload[at++] = (uint8_t)filler[((5U * case_index) + (3U * rep)
                                       + (7U * half) + index) % 26U];
    }
    payload[at++] = '\r';
    payload[at++] = '\n';
  }
}

// Queue the payload, record what the call starts from, make the call between
// two DWT reads, and record what it left behind. Runs in whatever context the
// case asks for, so it prints nothing: a hang would never reach a print.
static void measured_step(void)
{
  const Row *row = &rows[state.case_index];
  uint32_t start;
  uint32_t stop;

  // A write case keeps its last byte back, so the measured write() finds the
  // ring full; 63 bytes are the most the 64-byte ring takes, so queuing itself
  // never waits for the interrupt, in any context.
  if (row->step_id == STEP_WRITE) {
    state.acc1 = (uint32_t)Serial.write(payload, (size_t)row->n - 1U);
  } else {
    state.acc1 = (uint32_t)Serial.write(payload, (size_t)row->n);
  }

  state.head0 = serial->tx_head;
  state.tail0 = serial->tx_tail;
  state.size0 = (uint32_t)serial->tx_size;
  state.sr0 = serial->uart->SR;
  state.cr1_0 = serial->uart->CR1;
  state.pm0 = __get_PRIMASK();
  state.bp0 = __get_BASEPRI();
  state.ipsr = __get_IPSR();
  state.prio_tim = NVIC_GetPriority(TIM3_IRQn);
  state.en0 = NVIC_GetEnableIRQ(serial->irq);
  state.cyc0 = DWT->CYCCNT;
  arm(row->step_id);

  IWatchdog.reload();
  start = DWT->CYCCNT;
  if (row->step_id == STEP_WRITE) {
    call_accepted = (uint32_t)Serial.write(payload + (row->n - 1U), 1);
  } else if (row->step_id == STEP_FLUSH) {
    Serial.flush();
    call_accepted = 0;
  } else {
    Serial.flush((uint32_t)row->tmo);
    call_accepted = 0;
  }
  stop = DWT->CYCCNT;

  // The ring first and right here: in thread mode with interrupts enabled the
  // transfer interrupt can move tx_tail again as soon as the call has returned,
  // and what this records has to be the state the call left behind.
  after_head = serial->tx_head;
  after_tail = serial->tx_tail;
  after_size = (uint32_t)serial->tx_size;
  after_sr = serial->uart->SR;
  after_cr1 = serial->uart->CR1;
  after_pm = __get_PRIMASK();
  after_bp = __get_BASEPRI();
  after_en = NVIC_GetEnableIRQ(serial->irq);
  call_cycles = stop - start;
  // The call returned, so no reset is expected from this repetition any more.
  state.magic = 0;
}

static void timer_handler(void)
{
  // One shot: stop the timer before anything else, so that no second update
  // event runs this body again when the handler returns. HAL_TIM_IRQHandler
  // has already cleared the update flag.
  timer->pause();
  NVIC_ClearPendingIRQ(TIM3_IRQn);
  measured_step();
  step_done = true;
}

static void timer_setup(void)
{
  // TIM3: on APB1 (90 MHz timer clock), with an interrupt line of its own, and
  // neither TIMER_TONE (TIM6) nor TIMER_SERVO (TIM7) of this variant. 90 MHz
  // over 90 is 1 MHz, so the update event comes 200 us after resume().
  //
  // A local static, so it is constructed on the first call to this function,
  // which runs after the clocks are up and the HAL is initialised. An object at
  // file scope would be constructed before main() does either, and the heap is
  // not needed for one timer.
  static HardwareTimer instance(TIM3);

  timer = &instance;
  timer->pause();
  timer->setPrescaleFactor(90);
  timer->setOverflow(200, TICK_FORMAT);
  timer->refresh();  // load the prescaler and reload shadow registers
  timer->attachInterrupt(timer_handler);
  timer->pause();    // the update interrupt is enabled again per repetition
}

static void run_in_timer_isr(uint32_t preempt_priority)
{
  step_done = false;
  timer->setCount(0);
  // Discard an update event left over from an earlier repetition, so that the
  // handler runs once and for this repetition.
  TIM3->SR = ~(uint32_t)TIM_SR_UIF;
  NVIC_ClearPendingIRQ(TIM3_IRQn);
  // The library's default is 14, which the UART interrupt (priority 1) would
  // preempt, so nothing would ever wait. 1 is the UART's own level, where
  // neither preempts the other; 0 holds off SysTick as well.
  timer->setInterruptPriority(preempt_priority, 0);
  timer->resume();
  while (!step_done) {
    // The handler makes the measured call. A call that never returns is ended
    // by the watchdog, which is why nothing reloads it here.
  }
  timer->pause();
}

static void run_repetition(void)
{
  const Row *row = &rows[state.case_index];

  IWatchdog.reload();
  emit(snprintf(text, sizeof(text),
                "%s pre case=%lu rep=%lu name=%s ctx=%s step=%s n=%lu tmo=%lu",
                TAG, (unsigned long)state.case_index, (unsigned long)state.rep,
                row->name, row->ctx, step_name(row->step_id),
                (unsigned long)row->n, (unsigned long)row->tmo));
  build_payload(state.case_index, state.rep, row->n);
  // The pre line is the last thing written to the ring before the payload, so
  // its tail is what a stale re-send of the whole ring shows after the payload.
  Serial.flush();
  state.hp = serial->tx_head;

  switch (row->ctx_id) {
    case CTX_A:
      __disable_irq();
      measured_step();
      __enable_irq();
      break;
    case CTX_B:
      run_in_timer_isr(1);
      break;
    case CTX_C:
      run_in_timer_isr(0);
      break;
    default:
      measured_step();
      break;
  }

  // Back in thread mode with interrupts enabled: let the hardware finish before
  // anything is written to the ring again. On the unmodified core a flush
  // timeout that fires leaves tx_head == tx_tail while the chunk in flight and
  // the stale re-send of the ring are still running, so Serial.flush() alone
  // returns at once and the post line below would be copied into the ring that
  // re-send is still reading from.
  while (serial_tx_active(serial) || (serial->tx_head != serial->tx_tail)) {
  }
  Serial.flush();

  // C5: after data, the next record starts on a fresh line. A record is read
  // only where a line begins, so the post line may not be appended to a data
  // line that a flush timeout cut in half. The ring is empty and idle here, so
  // every byte it still held has gone out and the last byte that left the chip
  // is the one before the tail those transfers ended at: where that is not a
  // line feed, this closes the line the drop left open. Nothing is printed
  // where the payload went out whole, which is every step but a flush timeout
  // that really dropped something, nor where a stale re-send of the whole ring
  // ended on the pre line's own line feed.
  if (serial->tx_buff[(serial->tx_tail + SERIAL_TX_BUFFER_SIZE - 1U)
                      % SERIAL_TX_BUFFER_SIZE] != '\n') {
    Serial.write((const uint8_t *)"\r\n", 2);
    Serial.flush();
  }

  IWatchdog.reload();
  emit(snprintf(text, sizeof(text),
                "%s post case=%lu rep=%lu ret=1 cycles=%lu acc1=%lu acc2=%lu "
                "hp=%lu head0=%lu tail0=%lu size0=%lu sr0=0x%lx cr1_0=0x%lx "
                "pm0=%lu bp0=%lu ipsr=%lu prio_tim=%lu en0=%lu cyc0=%lu "
                "pm1=%lu bp1=%lu en1=%lu head1=%lu tail1=%lu size1=%lu "
                "sr1=0x%lx cr1_1=0x%lx",
                TAG, (unsigned long)state.case_index, (unsigned long)state.rep,
                (unsigned long)call_cycles, (unsigned long)state.acc1,
                (unsigned long)call_accepted, (unsigned long)state.hp,
                (unsigned long)state.head0, (unsigned long)state.tail0,
                (unsigned long)state.size0, (unsigned long)state.sr0,
                (unsigned long)state.cr1_0, (unsigned long)state.pm0,
                (unsigned long)state.bp0, (unsigned long)state.ipsr,
                (unsigned long)state.prio_tim, (unsigned long)state.en0,
                (unsigned long)state.cyc0, (unsigned long)after_pm,
                (unsigned long)after_bp, (unsigned long)after_en,
                (unsigned long)after_head, (unsigned long)after_tail,
                (unsigned long)after_size, (unsigned long)after_sr,
                (unsigned long)after_cr1));
}

// Prove, in this same run, that the watchdog resets this board, that .noinit
// survives that reset and that the reset flags say what caused it: spin without
// feeding the watchdog and store how long the spin lasted.
static void run_watchdog_control(void)
{
  uint32_t start;

  emit(snprintf(text, sizeof(text), "%s wdgarm rep=%lu", TAG,
                (unsigned long)state.wdg_rep));
  Serial.flush();
  IWatchdog.reload();
  state.spin_cycles = 0;
  arm(STEP_WDGCTL);
  start = DWT->CYCCNT;
  for (;;) {
    state.spin_cycles = DWT->CYCCNT - start;
  }
}

void setup()
{
  uint32_t csr;
  uint32_t armed_boot;
  uint32_t armed_step;
  bool valid;
  bool resumed;
  bool wdg_only;
  bool sft_only;

  // Read the reset flags before anything can clear them, then clear them: they
  // outlive the reset they describe.
  csr = RCC->CSR;
  RCC->CSR |= CSR_RMVF;

  armed_boot = state.boot;
  armed_step = state.step;
  valid = (state.magic == STATE_MAGIC)
          && (state.check == state_check())
          && (armed_step <= STEP_FLUSHS)
          && (state.case_index < NCASES)
          && (state.rep < REPS)
          && (state.wdg_rep <= WDG_REPS);
  wdg_only = ((csr & CSR_IWDGRSTF) != 0UL) && ((csr & CSR_SFTRSTF) == 0UL);
  sft_only = ((csr & CSR_SFTRSTF) != 0UL) && ((csr & CSR_IWDGRSTF) == 0UL);
  // A marker counts only together with the reset cause the step it was armed
  // for expects. A reset from outside the image lands in the same window -- the
  // bench's own reset, or a pin reset -- and must start the run from the
  // beginning. PINRSTF is never read: a system reset of either kind sets it too
  // on this series.
  resumed = valid && ((armed_step == STEP_NONE) ? sft_only : wdg_only);

  // Invalidate the marker at once. It is armed again only just before a call
  // that may hang, before the watchdog control's spin and in the few
  // instructions before the image's own reset, so a boot that stops anywhere
  // else leaves nothing behind for a later run.
  state.magic = 0;

  if (resumed) {
    state.boot = armed_boot + 1UL;
  } else {
    state.boot = 0;
    state.case_index = 0;
    state.rep = 0;
    state.wdg_rep = 0;
    state.step = STEP_NONE;
  }

  Serial.begin(115200);
  // The same arithmetic uart.c's get_serial_obj() does, in the other direction.
  serial = (serial_t *)((char *)Serial.getHandle() - offsetof(serial_t, handle));

  IWatchdog.begin(IWDG_PERIOD_US);
  while (IWDG->SR != 0UL) {
    // the prescaler and reload values are still being taken over
  }

  timer_setup();

  // C5: an empty line first, which ends whatever partial line the reset left on
  // the host's side before this start's first record.
  Serial.write((const uint8_t *)"\r\n", 2);
  emit(snprintf(text, sizeof(text),
                "%s boot image=hq180 boot=%lu resumed=%lu rcc_csr=0x%lx "
                "case=%lu rep=%lu sysclk=%lu hclk=%lu pclk1=%lu pclk2=%lu "
                "cfgr=0x%lx brr=0x%lx grouping=%lu prio_uart=%lu prio_tick=%lu "
                "tim_irqn=%lu timclk=%lu iwdg_pr=%lu iwdg_rlr=%lu dwt=%lu "
                "ring=%lu",
                TAG, (unsigned long)state.boot, (unsigned long)(resumed ? 1 : 0),
                (unsigned long)csr, (unsigned long)state.case_index,
                (unsigned long)state.rep,
                (unsigned long)HAL_RCC_GetSysClockFreq(),
                (unsigned long)HAL_RCC_GetHCLKFreq(),
                (unsigned long)HAL_RCC_GetPCLK1Freq(),
                (unsigned long)HAL_RCC_GetPCLK2Freq(),
                (unsigned long)RCC->CFGR, (unsigned long)serial->uart->BRR,
                (unsigned long)NVIC_GetPriorityGrouping(),
                (unsigned long)NVIC_GetPriority(serial->irq),
                (unsigned long)NVIC_GetPriority(SysTick_IRQn),
                (unsigned long)TIM3_IRQn,
                (unsigned long)timer->getTimerClkFreq(),
                (unsigned long)IWDG->PR, (unsigned long)IWDG->RLR,
                (unsigned long)((DWT->CTRL & DWT_CTRL_CYCCNTENA_Msk) ? 1 : 0),
                (unsigned long)SERIAL_TX_BUFFER_SIZE));

  if (resumed && (armed_step == STEP_WDGCTL)) {
    emit(snprintf(text, sizeof(text), "%s wdgctl rep=%lu cycles=%lu rcc_csr=0x%lx",
                  TAG, (unsigned long)state.wdg_rep,
                  (unsigned long)state.spin_cycles, (unsigned long)csr));
    state.wdg_rep += 1UL;
  } else if (resumed && (armed_step != STEP_NONE)) {
    // The call of the previous boot never returned. Its breadcrumbs say which
    // call it was and what the ring and the peripheral looked like.
    const Row *row = &rows[state.case_index];
    emit(snprintf(text, sizeof(text),
                  "%s wdg case=%lu rep=%lu name=%s ctx=%s step=%s acc1=%lu "
                  "hp=%lu head0=%lu tail0=%lu size0=%lu sr0=0x%lx cr1_0=0x%lx "
                  "pm0=%lu bp0=%lu ipsr=%lu prio_tim=%lu en0=%lu cyc0=%lu "
                  "boot=%lu",
                  TAG, (unsigned long)state.case_index, (unsigned long)state.rep,
                  row->name, row->ctx, step_name(armed_step),
                  (unsigned long)state.acc1, (unsigned long)state.hp,
                  (unsigned long)state.head0, (unsigned long)state.tail0,
                  (unsigned long)state.size0, (unsigned long)state.sr0,
                  (unsigned long)state.cr1_0, (unsigned long)state.pm0,
                  (unsigned long)state.bp0, (unsigned long)state.ipsr,
                  (unsigned long)state.prio_tim, (unsigned long)state.en0,
                  (unsigned long)state.cyc0, (unsigned long)armed_boot));
    state.rep += 1UL;  // that repetition is finished; the next one runs
  }

  if (state.wdg_rep < WDG_REPS) {
    run_watchdog_control();  // never returns: the watchdog resets the board
  }

  while (state.rep < REPS) {
    run_repetition();
    state.rep += 1UL;
  }

  if ((state.case_index + 1UL) < NCASES) {
    // Flush first, so the last record has left the shift register before any
    // marker exists, then arm and reset with nothing in between.
    Serial.flush();
    state.case_index += 1UL;
    state.rep = 0;
    arm(STEP_NONE);
    __DSB();
    NVIC_SystemReset();
  }

  emit(snprintf(text, sizeof(text), "%s done", TAG));
  Serial.flush();
}

void loop()
{
  // C6: after `done` the image prints nothing more, and it keeps feeding the
  // watchdog so that the board does not restart and print a second time.
  IWatchdog.reload();
}
