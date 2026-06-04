#include <iostream>
int main(){
    long x, best, cur;
    if(!(std::cin >> x)) return 0;
    best = cur = x;
    while(std::cin >> x){
        cur = (x > cur + x) ? x : cur + x;
        if(cur > best) best = cur;
    }
    std::cout << best << "\n";
    return 0;
}
