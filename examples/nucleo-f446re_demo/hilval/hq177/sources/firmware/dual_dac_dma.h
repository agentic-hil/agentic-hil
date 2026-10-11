/*
 * The dual-channel DAC example of libopencm3 issue #528, written for the
 * F2/F4 stream DMA controller instead of the channel one the library's
 * documentation shows. README.md in this directory says how the body of
 * dual_dac_dma_start() and the documentation patch are tied together.
 */
#ifndef HQ177_DUAL_DAC_DMA_H
#define HQ177_DUAL_DAC_DMA_H

#include <stdint.h>

void dual_dac_dma_start(uint32_t *words, const uint16_t *ch1,
			const uint16_t *ch2, uint16_t count,
			uint32_t prescaler, uint32_t period);

#endif
