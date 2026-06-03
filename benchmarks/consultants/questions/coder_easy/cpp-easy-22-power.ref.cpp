#include <iostream>
int main() {
    long long a, b;
    std::cin >> a >> b;
    long long r = 1;
    for (long long i = 0; i < b; i++) r *= a;
    std::cout << r << std::endl;
    return 0;
}
